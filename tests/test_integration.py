"""Smoke tests against a real Home Assistant core (needs pytest-homeassistant-custom-component)."""
import time
import copy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lego_tracker.const import DOMAIN, MAX_HISTORY
from custom_components.lego_tracker.parsers import Parsed

CSV = "Number;Name;Theme;Qty;Paid;Value\n10281-1;Bonsai;Botanicals;1;40;50\n42143;Ferrari;Technic;1;350;400\n"


@pytest.fixture
def entry(hass):
    e = MockConfigEntry(domain=DOMAIN, data={}, options={"discount_threshold": 25, "retailers": ["bol", "amazon_nl"],
                                                          "digest_time": "08:00:00", "min_history_days": 0,
                                                          # most tests run full rounds (off by default, see test_full_refresh_*)
                                                          "dev_full_refresh": True, "dev_fixed_times": True, "dev_free_cycle": True})
    e.add_to_hass(hass)
    return e


@pytest.fixture(autouse=True)
def no_refresh_gap():
    with patch("custom_components.lego_tracker.coordinator.FULL_REFRESH_GAP", 0):
        yield


@pytest.fixture(autouse=True)
def no_manual_gap():
    with patch("custom_components.lego_tracker.coordinator.MANUAL_GAP", 0):
        yield


@pytest.fixture(autouse=True)
def no_first_check(request):
    """Most tests count every fetch: the first check right after adding a set only runs where a test asks for it."""
    if request.node.get_closest_marker("first_check"):
        yield
        return
    with patch("custom_components.lego_tracker.coordinator.LegoCoordinator.queue_first_check"):
        yield


@pytest.fixture(autouse=True)
def no_catalogue():
    """Tests use real set numbers: without this the built-in catalogue would fill them in."""
    from custom_components.lego_tracker import catalog

    with patch.object(catalog, "_SETS", {}), patch.object(catalog, "load", lambda: {}):
        yield


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
        "refresh_mode": "times", "refresh_times": "8:00, 20.15", "language": "nl", "spread_hours": 12})
    assert out["type"] == "create_entry"
    assert entry.options["refresh_mode"] == "times" and entry.options["language"] == "nl" and entry.options["spread_hours"] == 12
    await hass.async_block_till_done()          # reload must not raise on re-registering panel/services
    assert entry.state.value == "loaded"


async def test_config_flow(hass: HomeAssistant):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["type"] == "form"
    keys = {str(k) for k in r["data_schema"].schema}
    assert "refresh_times" not in keys and "watch_cycle_min" in keys          # fixed times only in developer mode
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {
        "discount_threshold": 30, "retailers": ["bol", "kruidvat_be"], "digest_time": "08:00:00", "min_history_days": 3,
        "refresh_mode": "spread", "spread_hours": "12", "watch_cycle_min": "60"})
    assert r["type"] == "create_entry" and r["options"]["discount_threshold"] == 30
    assert r["options"]["refresh_mode"] == "spread" and r["options"]["language"] == "en" and r["options"]["auto_refresh"]
    assert r["options"]["spread_hours"] == 12 and r["options"]["watch_cycle_min"] == 60


async def test_config_flow_fixed_times_in_developer_mode(hass: HomeAssistant):
    from custom_components.lego_tracker.config_flow import InvalidTimes, _clean, _schema

    keys = {str(k) for k in _schema({"dev_fixed_times": True}).schema}
    assert "refresh_times" in keys
    with pytest.raises(InvalidTimes):
        _clean({"refresh_mode": "times", "refresh_times": "nooit"})
    assert _clean({"refresh_mode": "times", "refresh_times": "19:30, 7:30"})["refresh_times"] == "07:30, 19:30"


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
    assert notes and "target price" in notes[0].data["message"]
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


@pytest.mark.parametrize("merge", [False, True])
async def test_backup_history_validation_is_atomic(hass: HomeAssistant, entry, merge):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coord = hass.data[DOMAIN][entry.entry_id]
    before = copy.deepcopy(coord.store)
    for history in ([[i, 50] for i in range(MAX_HISTORY + 1)], [[2, 50], [1, 40]]):
        data = {"sets": {"10281": {}}, "offers": {"10281": {"bol": {"history": history}}}}
        with patch.object(coord, "push_update") as push:
            with pytest.raises(ServiceValidationError, match="invalid price history"):
                await hass.services.async_call(DOMAIN, "import_data", {"data": data, "merge": merge}, blocking=True)
            push.assert_not_called()
        assert coord.store == before


@pytest.mark.parametrize("merge", [False, True])
async def test_backup_history_limit_and_overview(hass: HomeAssistant, entry, hass_ws_client, merge):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    history = [[1_700_000_000 + i, 50 + i % 2] for i in range(MAX_HISTORY)]
    data = {"sets": {"10281": {"set_number": "10281"}},
            "offers": {"10281": {"bol": {"history": history, "available": True, "last_price": 51}}}}
    await hass.services.async_call(DOMAIN, "import_data", {"data": data, "merge": merge}, blocking=True)
    coord = hass.data[DOMAIN][entry.entry_id]
    assert coord.store["offers"]["10281"]["bol"]["history"] == history
    assert coord.store["offers"]["10281"]["bol"]["history"] is not history
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/overview"})
    card = (await ws.receive_json())["result"]["sets"][0]
    assert card["all_time_low"] == 50
    assert card["spark"] == [p for _, p in history][-60:]
    await ws.send_json({"id": 2, "type": "lego_tracker/set", "set_number": "10281"})
    assert (await ws.receive_json())["result"]["history"]["bol"] == history


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
    assert "future" in (await ws.receive_json())["error"]["message"]
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
    assert "suspicious price" in ov["sets"][0]["offers"]["bol"]["error"] and ov["sets"][0]["best_price"] is None
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
    with pytest.raises(ServiceValidationError, match="A job is already running"):
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
    assert offer["link_status"] == "suspect" and "wrong product" in offer["link_reason"]
    no_network.return_value = (Parsed(price=19.99, title="Led-verlichting voor LEGO 21028"), None)
    await hass.services.async_call(DOMAIN, "refresh", {"set_number": "21028"}, blocking=True, return_response=True)
    assert c.compute()["statuses"]["21028"]["best_price"] is None        # suspect link never counts
    # confirm overrides, remove blocks rediscovery of the same page
    await hass.services.async_call(DOMAIN, "confirm_offer", {"set_number": "21028", "retailer": "amazon_nl"}, blocking=True)
    assert c.store["offers"]["21028"]["amazon_nl"]["link_status"] == "confirmed"
    await hass.services.async_call(DOMAIN, "remove_offer", {"set_number": "21028", "retailer": "amazon_nl"}, blocking=True)
    assert "amazon_nl" not in c.store["offers"]["21028"] and "name" not in c.store["sets"]["21028"]
    with patch("custom_components.lego_tracker.client.Fetcher.discover",
               AsyncMock(side_effect=lambda r, n, force=False, url=None: "https://www.amazon.nl/dp/B0LEDLEDLE" if r == "amazon_nl" else None)):
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
    hass.config_entries.async_update_entry(entry, options={**entry.options, "refresh_times": "19:30, 07:05", "refresh_mode": "times"})
    c = await _setup(hass, entry)
    assert c.refresh_times == [(7, 5), (19, 30)] and c.schedule_info()["times"] == ["07:05", "19:30"]
    assert c.next_refresh() > time.time()
    hass.config_entries.async_update_entry(entry, options={**entry.options, "refresh_mode": "off"})
    await hass.async_block_till_done()
    c = hass.data[DOMAIN][entry.entry_id]
    assert c.schedule_info()["next"] is None


async def test_settings_panel_roundtrip(hass: HomeAssistant, entry, hass_ws_client, no_network):
    c = await _setup(hass, entry)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/settings/get"})
    st = (await ws.receive_json())["result"]
    assert st["refresh_times"] == "07:30, 19:30" and st["keys"]["rebrickable_api_key"]["set"] is False
    assert any(s["id"] == "dreamland_be" for s in st["shops"])
    await ws.send_json({"id": 2, "type": "lego_tracker/settings/set", "fields": {"refresh_times": "nooit"}})
    assert "times as HH:MM" in (await ws.receive_json())["error"]["message"]
    await ws.send_json({"id": 3, "type": "lego_tracker/settings/set", "fields": {
        "discount_threshold": 30, "refresh_times": "6:00, 18.15", "rebrickable_api_key": "abcdef1234567890",
        "retailers": ["bol", "dreamland_be", "c_vandijk", "nope"], "no_autopause": ["bol"], "value_source": "import_first",
        "custom_shops": [{"id": "c_vandijk", "name": "Van Dijk", "domain": "vandijk.be", "search": "https://www.vandijk.be/zoek?q={query}"}]}})
    assert (await ws.receive_json())["result"]["saved"]
    await hass.async_block_till_done()
    c = hass.data[DOMAIN][entry.entry_id]                                    # entry reloaded
    assert c.threshold == 30 and c.refresh_times == [(6, 0), (18, 15)] and c.retailers == ["bol", "dreamland_be", "c_vandijk"]
    assert c.fetcher.no_autopause == {"bol"} and c.store["value_source"] == "import_first"
    await ws.send_json({"id": 4, "type": "lego_tracker/settings/get"})
    st = (await ws.receive_json())["result"]
    assert st["keys"]["rebrickable_api_key"] == {"set": True, "masked": "••••7890"}   # key never sent back
    # keys: absent = keep, "" = clear
    await ws.send_json({"id": 5, "type": "lego_tracker/settings/set", "fields": {"discount_threshold": 20}})
    await ws.receive_json(); await hass.async_block_till_done()
    assert entry.options["rebrickable_api_key"] == "abcdef1234567890"
    # no auto-pause for bol: a block does not pause it
    c = hass.data[DOMAIN][entry.entry_id]
    c.fetcher._note_block("bol"); c.fetcher._note_block("amazon_nl")
    assert c.fetcher.cooldown_left("bol") == 0 and c.fetcher.cooldown_left("amazon_nl") > 0
    await ws.send_json({"id": 6, "type": "lego_tracker/shop_action", "action": "resume", "retailer": "amazon_nl"})
    assert (await ws.receive_json())["result"]["paused"] == {}


async def test_pause_survives_reload(hass: HomeAssistant, entry, no_network):
    c = await _setup(hass, entry)
    c.fetcher._note_block("amazon_nl")
    c._save()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.data[DOMAIN][entry.entry_id].fetcher.cooldown_left("amazon_nl") > 3000


async def test_csv_update_job_and_title_from_userscript(hass: HomeAssistant, entry, no_network):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10311"}, blocking=True)
    await hass.services.async_call(DOMAIN, "set_offer", {"set_number": "10311", "retailer": "bol", "url": "https://www.bol.com/nl/nl/p/lego-10311/1/"}, blocking=True)
    with patch("custom_components.lego_tracker.coordinator.lookup_metadata",
               AsyncMock(return_value=({"name": "Orchid", "year": 2022, "pieces": 608, "theme": "Icons", "image": "https://i/x.jpg"}, "Rebrickable"))), \
         patch("custom_components.lego_tracker.coordinator.asyncio.sleep", AsyncMock()):
        res = await hass.services.async_call(DOMAIN, "import_collection", {
            "csv_text": "Number;Qty;Paid;Value\n10311;1;40;55\n", "update_after": True}, blocking=True, return_response=True)
        assert res["added"] == 1
        await hass.async_block_till_done(wait_background_tasks=True)
    assert c.last_job["kind"] == "update" and c.last_job["done"] == 1
    assert c.store["sets"]["10311"]["name"] == "Orchid" and c.compute()["statuses"]["10311"]["best_price"] == 30.0
    assert c.store["collection"]["10311"]["current_value"] == 55.0
    # userscript sends the page title -> link check judges it
    c.store["offers"]["10311"]["amazon_nl"] = {"url": "https://www.amazon.nl/dp/B0LEDLEDLE", "history": []}
    await hass.services.async_call(DOMAIN, "report_price", {"url": "https://www.amazon.nl/dp/B0LEDLEDLE?th=1", "price": 45.0,
                                                            "title": "LED verlichting voor LEGO 10311"}, blocking=True)
    assert c.store["offers"]["10311"]["amazon_nl"]["link_status"] == "suspect"


async def test_userscript_is_generated(hass: HomeAssistant, entry, hass_client_no_auth):
    await _setup(hass, entry)
    client = await hass_client_no_auth()
    resp = await client.get("/api/lego_tracker/lego-tracker.user.js")
    assert resp.status == 200
    text = await resp.text()
    assert "// ==UserScript==" in text and "@match        https://www.dreamland.be/*" in text
    assert "{{" not in text and "@updateURL" in text and "title: titleOf(document)" in text


async def test_lego_com_is_first_source(hass: HomeAssistant, entry, no_network):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10311"}, blocking=True)
    lego = Parsed(price=39.99, list_price=49.99, title="Orchidee 10311 | LEGO® Icons | Officiële LEGO® winkel BE",
                  image="https://www.lego.com/cdn/10311.png")
    meta = {"name": "Orchid", "rrp": 45.0, "image": "https://other/x.jpg", "year": 2022, "pieces": 608, "theme": "Icons"}
    with patch("custom_components.lego_tracker.client.Fetcher.discover",
               AsyncMock(side_effect=lambda r, n, force=False: "https://www.lego.com/nl-be/product/orchidee-10311" if r == "lego_com" else None)), \
         patch("custom_components.lego_tracker.client.Fetcher.fetch_offer", AsyncMock(return_value=(lego, None))), \
         patch("custom_components.lego_tracker.coordinator.lookup_metadata", AsyncMock(return_value=(meta, "Brickset"))), \
         patch("custom_components.lego_tracker.coordinator.asyncio.sleep", AsyncMock()):
        await hass.services.async_call(DOMAIN, "enrich_sets", {"set_number": "10311"}, blocking=True, return_response=True)
    s = c.store["sets"]["10311"]
    assert (s["rrp"], s["rrp_source"], s["image"], s["name"]) == (49.99, "LEGO.com", "https://www.lego.com/cdn/10311.png", "Orchidee")
    assert s["year"] == 2022                                           # the rest comes from the next source
    offer = c.store["offers"]["10311"]["lego_com"]
    assert offer["link_status"] == "ok" and offer["last_price"] == 39.99
    assert c.compute()["statuses"]["10311"]["discount_rrp"] == 20.0    # LEGO.com sale counts as a deal
    # a user-set RRP is never overwritten
    c.update_set("10311", {"rrp": 55})
    c._apply_lego("10311", lego)
    assert s["rrp"] == 55 and s["rrp_source"] == "user"


async def test_settings_search_templates(hass: HomeAssistant, entry, hass_ws_client):
    c = await _setup(hass, entry)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/settings/get"})
    shops = {s["id"]: s for s in (await ws.receive_json())["result"]["shops"]}
    assert shops["bol"]["search"] == shops["bol"]["default_search"] == "https://www.bol.com/nl/nl/s/?searchtext={query}"
    assert shops["lego_com"]["search"].startswith("https://www.lego.com/{locale}/")
    await ws.send_json({"id": 2, "type": "lego_tracker/settings/set", "fields": {"shop_search": {"bol": "ftp://x"}}})
    assert (await ws.receive_json())["error"]["code"] == "invalid_format"
    await ws.send_json({"id": 3, "type": "lego_tracker/settings/set", "fields": {
        "lego_locale": "nl-NL", "shop_search": {"bol": "https://www.bol.com/be/nl/s/?searchtext={query}",
                                                "amazon_nl": "https://www.amazon.nl/s?k={query}"}}})
    assert (await ws.receive_json())["result"]["saved"]
    await hass.async_block_till_done()
    assert entry.options["shop_search"] == {"bol": "https://www.bol.com/be/nl/s/?searchtext={query}"}   # defaults not stored
    from custom_components.lego_tracker.parsers import search_url
    assert search_url("bol", "1") == "https://www.bol.com/be/nl/s/?searchtext=LEGO+1"
    assert search_url("lego_com", "1") == "https://www.lego.com/nl-nl/search?q=1"


# ---------------------------------------------------------------- 0.8.0: notifications + errors
from custom_components.lego_tracker.notifications import in_quiet, set_triggers, validate_rules  # noqa: E402


def _rule(**kw):
    base = {"name": "r", "scope": {"type": "all"}, "triggers": ["all_time_low"], "targets": [{"type": "persistent"}]}
    base.update(kw)
    return validate_rules([base])[0]


def test_validate_rules_messages():
    with pytest.raises(ValueError, match="event"):
        validate_rules([{"name": "x", "triggers": [], "targets": [{"type": "persistent"}]}])
    with pytest.raises(ValueError, match="value"):
        validate_rules([{"name": "x", "triggers": ["price_below"], "targets": [{"type": "persistent"}]}])
    with pytest.raises(ValueError, match="e-mail"):
        validate_rules([{"name": "x", "triggers": ["digest"], "targets": [{"type": "email", "service": "notify.smtp", "to": "nope"}]}])
    with pytest.raises(ValueError, match="theme"):
        validate_rules([{"name": "x", "scope": {"type": "themes"}, "triggers": ["digest"], "targets": [{"type": "persistent"}]}])
    r = _rule(scope={"type": "sets", "sets": ["10311-1", "abc", "42143"]}, triggers=["price_below", "bogus"],
              params={"price_below": "35"}, targets=[{"type": "email", "service": "notify.smtp", "to": "a@b.be; c@d.nl"}])
    assert r["scope"]["sets"] == ["10311", "42143"]
    assert r["triggers"] == ["price_below"] and r["params"]["price_below"] == 35 and r["targets"][0]["to"] == ["a@b.be", "c@d.nl"]


def test_set_triggers_matrix():
    r = _rule(triggers=["all_time_low", "discount", "target_hit", "price_below", "price_drop", "deal_score", "back_in_stock"],
              params={"discount_pct": 30, "price_below": 40, "drop_pct": 10, "min_score": 70})
    before = {"best_price": 50.0, "discount_rrp": 0, "deal_score": 20}
    after = {"best_price": 35.0, "discount_rrp": 30, "is_all_time_low": True, "target_hit": True, "deal_score": 75}
    got = {t for t, _ in set_triggers(r, before, after)}
    assert got == {"all_time_low", "discount", "target_hit", "price_below", "price_drop", "deal_score"}
    assert {t for t, _ in set_triggers(r, {}, {"best_price": 60.0})} == {"back_in_stock"}
    assert set_triggers(r, after, after) == []                                        # nothing new
    shop_only = _rule(triggers=["any_change"], shops=["bol"])
    assert set_triggers(shop_only, {"best_price": 50}, {"best_price": 45, "best_retailer": "amazon_nl"}) == []


def test_quiet_hours():
    from datetime import datetime
    q = {"from": "22:00", "to": "07:00"}
    assert in_quiet(q, datetime(2026, 1, 1, 23, 30)) and in_quiet(q, datetime(2026, 1, 1, 6, 59))
    assert not in_quiet(q, datetime(2026, 1, 1, 7, 0)) and not in_quiet(None, datetime(2026, 1, 1, 3, 0))


async def test_rules_scope_targets_cooldown_and_quiet(hass: HomeAssistant, entry, no_network, hass_ws_client):
    from pytest_homeassistant_custom_component.common import async_mock_service

    c = await _setup(hass, entry)
    mobile = async_mock_service(hass, "notify", "mobile_app_pixel")
    smtp = async_mock_service(hass, "notify", "smtp_gmail")
    send_msg = async_mock_service(hass, "notify", "send_message")
    tts = async_mock_service(hass, "tts", "speak")
    for n, theme in (("10311", "Icons"), ("42143", "Technic")):
        await hass.services.async_call(DOMAIN, "add_set", {"set_number": n, "theme": theme, "rrp": 50}, blocking=True)
        await hass.services.async_call(DOMAIN, "set_offer", {"set_number": n, "retailer": "bol", "url": f"https://www.bol.com/nl/nl/p/lego-{n}/1/"}, blocking=True)
    c.store["sets"]["10311"]["image"] = "https://img/10311.png"
    ws = await hass_ws_client(hass)
    rules = [
        {"name": "Icons onder 35", "scope": {"type": "themes", "themes": ["Icons"]}, "triggers": ["price_below"],
         "params": {"price_below": 35}, "targets": [{"type": "mobile", "service": "notify.mobile_app_pixel"},
                                                    {"type": "email", "service": "notify.smtp_gmail", "to": "me@example.com"}]},
        {"name": "Alles naar speaker", "scope": {"type": "all"}, "triggers": ["any_change", "back_in_stock"],
         "targets": [{"type": "tts", "tts": "tts.google", "media_player": "media_player.keuken"},
                     {"type": "entity", "entity_id": "notify.telegram"}], "cooldown_hours": 0},
        {"name": "Nachtrust", "scope": {"type": "sets", "sets": ["42143"]}, "triggers": ["back_in_stock"],
         "targets": [{"type": "mobile", "service": "notify.mobile_app_pixel"}], "quiet": {"from": "00:00", "to": "23:59"}},
    ]
    await ws.send_json({"id": 1, "type": "lego_tracker/notify/set", "rules": rules})
    saved = (await ws.receive_json())["result"]["rules"]
    assert len(saved) == 3
    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)          # both sets get €30 (mock)
    await hass.async_block_till_done(wait_background_tasks=True)
    # theme scope: only 10311 (Icons) -> one mobile + one email, with image/link data
    assert len(mobile) == 1 and "10311" in mobile[0].data["title"]
    assert mobile[0].data["data"]["image"] == "https://img/10311.png" and "url" in mobile[0].data["data"]
    assert smtp[0].data["target"] == ["me@example.com"] and "<img" in smtp[0].data["data"]["html"]
    # all-scope rule: both sets, tts + notify entity
    assert len(tts) == 2 and tts[0].data["media_player_entity_id"] == "media_player.keuken"
    assert len(send_msg) == 2 and send_msg[0].data["entity_id"] == "notify.telegram"
    # quiet hours: 42143 queued, not pushed
    assert c.store["notify_queue"][saved[2]["id"]]
    # cooldown: same trigger again for rule 1 does not re-send
    c.store["offers"]["10311"]["bol"]["history"][-1][1] = 40.0
    c.store["offers"]["10311"]["bol"]["last_price"] = 40.0
    await c.notifier.on_set_change("10311", {"best_price": 40.0}, {"best_price": 30.0, "best_retailer": "bol"})
    assert len(mobile) == 1
    # test button + options for the dropdowns
    await ws.send_json({"id": 2, "type": "lego_tracker/notify/test", "rule": rules[0]})
    res = (await ws.receive_json())["result"]["results"]
    assert all(r["ok"] for r in res) and len(mobile) == 2
    await ws.send_json({"id": 3, "type": "lego_tracker/notify/get"})
    got = (await ws.receive_json())["result"]
    kinds = {n["service"]: n["kind"] for n in got["options"]["notify"]}
    assert kinds["notify.mobile_app_pixel"] == "mobile" and kinds["notify.smtp_gmail"] == "email"
    assert "Icons" in got["options"]["themes"] and got["log"] and got["options"]["triggers"]["price_below"]["param"] == "price_below"


async def test_default_rules_and_digest(hass: HomeAssistant, entry, no_network):
    from pytest_homeassistant_custom_component.common import async_mock_service
    from custom_components.lego_tracker import _send_digest

    c = await _setup(hass, entry)
    assert [r["id"] for r in c.store["notify_rules"]] == ["deals", "digest"]
    pn = async_mock_service(hass, "persistent_notification", "create")
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10311", "rrp": 50}, blocking=True)
    await hass.services.async_call(DOMAIN, "set_offer", {"set_number": "10311", "retailer": "bol", "url": "https://www.bol.com/nl/nl/p/lego-10311/1/"}, blocking=True)
    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    n_before = len(pn)
    await _send_digest(hass, c)
    await hass.async_block_till_done()
    assert len(pn) == n_before + 1 and "10311" in pn[-1].data["message"]


async def test_fix_offer(hass: HomeAssistant, entry, no_network):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "21028", "rrp": 49.99}, blocking=True)
    c.store["offers"]["21028"]["amazon_nl"] = {"url": "https://www.amazon.nl/dp/B0LEDLEDLE", "history": [[1, 19.99]],
                                               "link_status": "suspect", "error": "blocked (HTTP 403)"}
    await hass.services.async_call(DOMAIN, "fix_offer", {"set_number": "21028", "retailer": "amazon_nl",
                                                         "url": "https://www.amazon.nl/LEGO/dp/B0GOODGOOD", "price": 42.5}, blocking=True)
    o = c.store["offers"]["21028"]["amazon_nl"]
    assert o["url"] == "https://www.amazon.nl/dp/B0GOODGOOD" and o["link_status"] == "confirmed"
    assert o["history"][-1][1] == 42.5 and len(o["history"]) == 1 and o["error"] is None      # old wrong history gone
    assert c.compute()["statuses"]["21028"]["best_price"] == 42.5
    # price only, same link
    await hass.services.async_call(DOMAIN, "fix_offer", {"set_number": "21028", "retailer": "amazon_nl", "price": 41.0}, blocking=True)
    assert c.compute()["statuses"]["21028"]["best_price"] == 41.0
    with pytest.raises(ServiceValidationError, match="link"):
        await hass.services.async_call(DOMAIN, "fix_offer", {"set_number": "21028", "retailer": "bol", "price": 40}, blocking=True)


async def test_logbook_records_everything(hass: HomeAssistant, entry, no_network, hass_ws_client):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10311", "rrp": 50}, blocking=True)
    await hass.services.async_call(DOMAIN, "set_offer", {"set_number": "10311", "retailer": "bol", "url": "https://www.bol.com/nl/nl/p/lego-10311/1/"}, blocking=True)
    await hass.services.async_call(DOMAIN, "set_offer", {"set_number": "10311", "retailer": "amazon_nl", "url": "B08XYZ1234"}, blocking=True)
    no_network.side_effect = lambda rid, url, force=False: (Parsed(price=30.0), None) if rid == "bol" else (None, "blocked (HTTP 403)")
    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.services.async_call(DOMAIN, "report_price", {"url": "https://www.amazon.nl/dp/B08XYZ1234", "price": 33.0}, blocking=True)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "report_price", {"url": "https://www.amazon.nl/dp/B0UNKNOWN1", "price": 10.0}, blocking=True)
    c.fetcher._note_block("amazon_nl")                    # real pause path
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/log"})
    log = (await ws.receive_json())["result"]
    kinds = {e["kind"] for e in log["entries"]}
    assert {"link", "job", "price", "fetch", "check", "userscript", "shop"} <= kinds
    msgs = " | ".join(e["message"] for e in log["entries"])
    assert "first price €30.00" in msgs and "Amazon.nl blocked us" in msgs
    assert "bol.com: 1 succeeded, 0 failed" in msgs and "Amazon.nl: 0 succeeded, 1 failed" in msgs
    check = next(e for e in log["entries"] if e["kind"] == "check")
    assert check["results"]["bol"]["ok"] is True and check["results"]["amazon_nl"]["error"] == "blocked (HTTP 403)"
    assert check["level"] == "warning" and check["message"] == "1 of 2 shops OK"
    await ws.send_json({"id": 2, "type": "lego_tracker/log", "source": "userscript"})
    us = (await ws.receive_json())["result"]["entries"]
    assert any(e["message"] == "open error solved by your browser" for e in us)       # the link had an error before
    us = [e for e in us if e["message"] != "open error solved by your browser"]
    assert len(us) == 2 and {e["level"] for e in us} == {"ok", "warning"}
    await ws.send_json({"id": 3, "type": "lego_tracker/log", "level": "problems", "retailer": "amazon_nl"})
    assert all(e["level"] in ("error", "warning") and (e.get("retailer") == "amazon_nl" or "amazon_nl" in (e.get("results") or {})) for e in (await ws.receive_json())["result"]["entries"])


async def test_manual_link_and_price_win_until_cleared(hass: HomeAssistant, entry, no_network, hass_ws_client):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281", "rrp": 49.99}, blocking=True)
    ws = await hass_ws_client(hass)
    # add a link by hand for a shop without one
    await ws.send_json({"id": 1, "type": "lego_tracker/offer/update", "set_number": "10281", "retailer": "bol",
                        "url": "https://www.bol.com/nl/nl/p/lego-bonsai/9300000012345/"})
    card = (await ws.receive_json())["result"]
    bol = card["offers"]["bol"]
    assert bol["manual_url"] and bol["link_status"] == "confirmed"
    # a manual price wins over the automatic one and survives a refresh
    await ws.send_json({"id": 2, "type": "lego_tracker/offer/update", "set_number": "10281", "retailer": "bol", "manual_price": "35.5"})
    assert (await ws.receive_json())["success"]
    await c.refresh_all()
    o = c.store["offers"]["10281"]["bol"]
    assert o["manual_price"]["price"] == 35.5 and o["auto_price"] == 30.0
    assert c.compute()["statuses"]["10281"]["best_price"] == 35.5
    # clearing the manual price hands the field back to automation
    await ws.send_json({"id": 3, "type": "lego_tracker/offer/update", "set_number": "10281", "retailer": "bol", "manual_price": None})
    card = (await ws.receive_json())["result"]
    assert c.compute()["statuses"]["10281"]["best_price"] == 30.0 and "manual_price" not in o
    # discover never replaces a manual link
    assert c._missing("10281", ["bol"]) == []
    # an invalid price is refused with a readable error
    await ws.send_json({"id": 4, "type": "lego_tracker/offer/update", "set_number": "10281", "retailer": "bol", "manual_price": "abc"})
    assert (await ws.receive_json())["error"]["message"] == "Invalid price."
    # a price without a link is refused
    await ws.send_json({"id": 5, "type": "lego_tracker/offer/update", "set_number": "10281", "retailer": "amazon_nl", "manual_price": 20})
    assert "link" in (await ws.receive_json())["error"]["message"]
    # clearing the link removes it without blocking it
    c.store.setdefault("rejected", {})["10281"] = ["other-rejected"]
    await ws.send_json({"id": 6, "type": "lego_tracker/offer/update", "set_number": "10281", "retailer": "bol", "url": ""})
    assert (await ws.receive_json())["success"]
    assert "bol" not in c.store["offers"]["10281"] and c.store["rejected"]["10281"] == ["other-rejected"]
    msgs = [e["message"] for e in c.store["activity"]]
    assert any("manual price €35.50" in m for m in msgs) and any("link cleared" in m for m in msgs)


async def test_cleared_set_fields_are_refilled(hass: HomeAssistant, entry, no_network):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281", "name": "My name", "rrp": 49.99}, blocking=True)
    c.update_set("10281", {"name": "Custom", "rrp": 55})
    s = c.store["sets"]["10281"]
    assert s["name_source"] == "user" and s["rrp_source"] == "user"
    with patch.object(c, "enrich_set", AsyncMock()) as enrich:
        c.update_set("10281", {"rrp": ""})
        await hass.async_block_till_done(wait_background_tasks=True)
    assert "rrp" not in s and "rrp_source" not in s and s["name"] == "Custom"
    enrich.assert_awaited_once_with("10281")


async def test_spread_scheduler(hass: HomeAssistant, entry, no_network):
    c = await _setup(hass, entry)
    assert c.refresh_mode == "spread"
    for n in ("10281", "42143", "21028"):
        await hass.services.async_call(DOMAIN, "add_set", {"set_number": n}, blocking=True)
        await hass.services.async_call(DOMAIN, "set_offer", {"set_number": n, "retailer": "bol",
                                                             "url": f"https://www.bol.com/nl/nl/p/x/{n}00/"}, blocking=True)
    assert c.spread_interval() == 24 * 3600 / 3
    hass.config_entries.async_update_entry(entry, options={**entry.options, "spread_hours": 1})
    c = hass.data[DOMAIN][entry.entry_id] if entry.entry_id in hass.data[DOMAIN] else c
    await hass.async_block_till_done()
    c = hass.data[DOMAIN][entry.entry_id]
    assert c.spread_interval() == 1200
    c.store["sets"]["42143"]["checked"] = 1          # oldest check goes first
    c.store["sets"]["10281"]["checked"] = time.time()
    c.store["sets"]["21028"]["checked"] = 5
    assert c.next_spread_set() == "42143"
    await c._spread_tick()
    assert c.store["sets"]["42143"]["checked"] > 1 and c.next_spread_set() == "21028"
    check = [e for e in c.store["activity"] if e["kind"] == "check"][-1]
    assert check["source"] == "schedule" and check["results"]["bol"]["ok"] and check["set_number"] == "42143"
    info = c.schedule_info()
    assert info["mode"] == "spread" and info["per_hour"] == 3.0 and info["checked_24h"] == 2 and info["total"] == 3
    for n in c.store["sets"]:
        c.store["sets"][n]["checked"] = time.time()
    assert c.next_spread_set() is None                # all recently checked: nothing to do
    c.stop_spread()


async def test_log_status_filter(hass: HomeAssistant, entry, no_network, hass_ws_client):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281"}, blocking=True)
    for rid in ("bol", "amazon_nl"):
        await hass.services.async_call(DOMAIN, "set_offer", {"set_number": "10281", "retailer": rid,
                                                             "url": "https://www.bol.com/nl/nl/p/x/1/" if rid == "bol" else "https://www.amazon.nl/dp/B0AAAAAAAA"}, blocking=True)
    no_network.side_effect = lambda rid, url, force=False: (Parsed(price=30.0), None) if rid == "bol" else (None, "blocked (HTTP 403)")
    await c.refresh_all()
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/log", "kind": "check", "retailer": "amazon_nl", "status": "fail"})
    res = (await ws.receive_json())["result"]
    assert len(res["entries"]) == 1 and res["facets"]["retailer"].get("amazon_nl")
    await ws.send_json({"id": 2, "type": "lego_tracker/log", "kind": "check", "retailer": "amazon_nl", "status": "ok"})
    assert (await ws.receive_json())["result"]["entries"] == []
    await ws.send_json({"id": 3, "type": "lego_tracker/log", "kind": "check", "retailer": "bol", "status": "ok"})
    assert len((await ws.receive_json())["result"]["entries"]) == 1


async def test_language_setting_translates_outbound_texts(hass: HomeAssistant, entry, hass_client_no_auth, hass_ws_client):
    from custom_components.lego_tracker import i18n

    c = await _setup(hass, entry)
    assert c.language == "en" and i18n.tr("Shop paused") == "Shop paused"
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/settings/set", "fields": {"language": "nl"}})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    c = hass.data[DOMAIN][entry.entry_id]
    assert c.language == "nl" and i18n.tr("Shop paused") != "Shop paused"
    assert i18n.tr("€{price} at {shop}", price="9.99", shop="bol.com").startswith("€9.99")
    with pytest.raises(ValueError, match="niet"):
        c.update_offer("99999", "bol", manual_price=1)
    client = await hass_client_no_auth()
    text = await (await client.get("/api/lego_tracker/lego-tracker.user.js")).text()
    assert "{{" not in text and "LEGO Price Tracker" in text
    await ws.send_json({"id": 2, "type": "lego_tracker/settings/set", "fields": {"language": "xx"}})
    assert not (await ws.receive_json())["success"]
    i18n.set_language("en")


async def test_fetch_one_shop_now_even_when_paused(hass: HomeAssistant, entry, no_network, hass_ws_client):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281", "rrp": 49.99}, blocking=True)
    await hass.services.async_call(DOMAIN, "set_offer", {"set_number": "10281", "retailer": "bol",
                                                         "url": "https://www.bol.com/nl/nl/p/x/1/"}, blocking=True)
    c.fetcher._note_block("bol")                                   # shop paused
    assert c.fetcher.cooldown_left("bol") > 0
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/offer/fetch", "set_number": "10281", "retailer": "bol"})
    res = (await ws.receive_json())["result"]
    assert res["result"] == {"ok": True, "found": False, "price": 30.0, "error": None}
    assert res["set"]["offers"]["bol"]["price"] == 30.0 and "history" in res["set"]
    check = [e for e in c.store["activity"] if e["kind"] == "check"][-1]
    assert check["source"] == "panel" and check["results"]["bol"]["ok"]
    # no link yet: the shop is searched first
    with patch.object(c.fetcher, "discover", AsyncMock(return_value="https://www.amazon.nl/dp/B0FOUND001")) as disc:
        await ws.send_json({"id": 2, "type": "lego_tracker/offer/fetch", "set_number": "10281", "retailer": "amazon_nl"})
        res = (await ws.receive_json())["result"]["result"]
    assert res["found"] and res["ok"] and disc.await_args.kwargs.get("force") is True
    # nothing found: a readable reason, no offer created
    await ws.send_json({"id": 3, "type": "lego_tracker/offer/fetch", "set_number": "10281", "retailer": "kruidvat_be"})
    res = (await ws.receive_json())["result"]["result"]
    assert res == {"ok": False, "found": False, "error": "no matching product found"} and "kruidvat_be" not in c.store["offers"]["10281"]
    await ws.send_json({"id": 4, "type": "lego_tracker/offer/fetch", "set_number": "99999", "retailer": "bol"})
    assert not (await ws.receive_json())["success"]


async def test_lego_com_image_replaces_other_images(hass: HomeAssistant, entry, no_network, hass_ws_client):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281"}, blocking=True)
    s = c.store["sets"]["10281"]
    s["image"], s["image_source"] = "https://shop.example/img.jpg", "shop"
    assert c.needs_enrich("10281")
    lego = Parsed(price=49.99, title="Bonsai Tree 10281 | LEGO", image="https://www.lego.com/cdn/10281.png", list_price=49.99)
    no_network.side_effect = lambda rid, url, force=False: (lego, None)
    with patch.object(c.fetcher, "discover", AsyncMock(return_value="https://www.lego.com/nl-be/product/bonsai-tree-10281")):
        ws = await hass_ws_client(hass)
        await ws.send_json({"id": 1, "type": "lego_tracker/set/enrich", "set_number": "10281"})
        res = (await ws.receive_json())["result"]
    assert res["set"]["image"] == "https://www.lego.com/cdn/10281.png" and s["image_source"] == "LEGO.com" and s["rrp"] == 49.99
    c.update_set("10281", {"image": "https://my.example/own.png"})
    await c.lego_lookup("10281", force=True)
    assert s["image"] == "https://my.example/own.png"                  # a manual image always wins


async def test_bol_api_finds_and_prices_without_scraping(hass: HomeAssistant, entry, no_network, aioclient_mock, hass_ws_client):
    import re as _re
    aioclient_mock.post("https://login.bol.com/token", json={"access_token": "tok", "expires_in": 299})
    aioclient_mock.get(_re.compile(r"https://api\.bol\.com/marketing/catalog/v1/products/search.*"), json={"results": [
        {"product": {"ean": "5702017000000", "title": "LEGO Technic 42143 lamp set", "url": "https://www.bol.com/nl/nl/p/lamp/111/"}, "offer": {"price": 9.99}},
        {"product": {"ean": "5702016912340", "title": "LEGO Icons Bonsaiboompje - 10281", "url": "https://www.bol.com/be/nl/p/lego-bonsai/9300000038297067/"},
         "offer": {"price": 37.99}}]})
    aioclient_mock.get(_re.compile(r"https://api\.bol\.com/marketing/catalog/v1/products/5702016912340/offers/best.*"),
                       json={"ean": "5702016912340", "price": 36.5, "strikethroughPrice": 49.99, "deliveryDescription": "Op voorraad"})
    hass.config_entries.async_update_entry(entry, options={**entry.options, "bol_client_id": "client-id-123", "bol_client_secret": "s3cr3t/key+=="})
    c = await _setup(hass, entry)
    assert c.bol_api and c.bol_country == "BE"                      # lego_locale nl-be
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281", "rrp": 49.99}, blocking=True)
    res = await c.fetch_shop("10281", "bol")
    o = c.store["offers"]["10281"]["bol"]
    assert res["ok"] and res["price"] == 36.5, (res, o)
    assert o["ean"] == "5702016912340" and o["url"].endswith("/9300000038297067/") and o["last_price"] == 36.5
    assert not no_network.called                                    # no scraping for bol.com
    # the relay leaves bol.com to the API
    o["error"] = "blocked (HTTP 403)"
    assert all(i["retailer"] != "bol" for i in c.relay_items()["items"])
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/settings/test_key", "source": "bol"})
    r = (await ws.receive_json())["result"]
    assert r["ok"] and "Bonsaiboompje" in r["message"]
    await ws.send_json({"id": 2, "type": "lego_tracker/settings/get"})
    st = (await ws.receive_json())["result"]
    assert st["bol_api"] and st["keys"]["bol_client_secret"]["set"] and "s3cr3t" not in str(st)


async def test_bol_api_bad_credentials_are_explained(hass: HomeAssistant, entry, no_network, aioclient_mock):
    aioclient_mock.post("https://login.bol.com/token", status=401)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "bol_client_id": "client-id-123", "bol_client_secret": "wrongwrong"})
    c = await _setup(hass, entry)
    ok, msg = await c.test_bol()
    assert not ok and msg == "bol.com API: client id or secret not accepted"
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281"}, blocking=True)
    c.store["offers"]["10281"]["bol"] = {"url": "https://www.bol.com/nl/nl/p/x/1/", "history": []}
    await c.refresh_set("10281", ["bol"])
    assert c.store["offers"]["10281"]["bol"]["error"] == "bol.com API: client id or secret not accepted"


async def test_browser_relay_list_and_results(hass: HomeAssistant, entry, no_network, hass_client):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281", "rrp": 49.99}, blocking=True)
    for rid, url in (("bol", "https://www.bol.com/nl/nl/p/lego-bonsai/9300000038297067/"), ("amazon_nl", "https://www.amazon.nl/dp/B0BONSAI01")):
        await hass.services.async_call(DOMAIN, "set_offer", {"set_number": "10281", "retailer": rid, "url": url}, blocking=True)
    c.store["offers"]["10281"]["amazon_nl"].update(last_ok=time.time(), error=None)      # fine on the server
    c.store["offers"]["10281"]["bol"]["error"] = "blocked (HTTP 403)"
    client = await hass_client()
    data = await (await client.get("/api/lego_tracker/relay")).json()
    assert data["enabled"] and [i["retailer"] for i in data["items"]] == ["bol"] and data["interval_hours"] == 6
    item = data["items"][0]
    r = await client.post("/api/lego_tracker/relay", json={"results": [{**item, "price": 36.99, "title": "LEGO Icons Bonsaiboompje 10281"}]})
    assert (await r.json()) == {"ok": 1, "fail": 0, "rejected": [], "follow": []}
    o = c.store["offers"]["10281"]["bol"]
    assert o["last_price"] == 36.99 and o["error"] is None
    relay_log = [e for e in c.store["activity"] if e.get("source") == "relay"]
    assert relay_log and "your browser" in relay_log[-1]["message"]
    r = await client.post("/api/lego_tracker/relay", json={"results": [{**item, "error": "blocked (captcha / bot protection)"}]})
    assert (await r.json())["fail"] == 1 and c.store["relay_last"]["ok"] == 1 and c.store["relay_last"]["fail"] == 1
    r = await client.post("/api/lego_tracker/relay", json={"results": [{**item, "price": 2.0}]})     # suspicious: rejected
    assert (await r.json())["rejected"]
    assert (await client.post("/api/lego_tracker/relay", data="nope")).status == 400
    hass.config_entries.async_update_entry(entry, options={**entry.options, "browser_relay": False})
    await hass.async_block_till_done()
    data = await (await client.get("/api/lego_tracker/relay")).json()
    assert not data["enabled"] and data["items"] == []


async def test_userscript_has_relay_and_ha_include(hass: HomeAssistant, entry, hass_client_no_auth):
    await _setup(hass, entry)
    text = await (await (await hass_client_no_auth()).get("/api/lego_tracker/lego-tracker.user.js")).text()
    assert "// @include      *://*/lego-tracker*" in text and "/api/lego_tracker/relay" in text and "{{" not in text


def _pages(**by_fragment):
    """get_page mock: (status, html) per URL fragment, 404 for the rest."""
    async def get(src, url, force=False, note_block=True):
        for frag, res in by_fragment.items():
            if frag in url:
                return res(url) if callable(res) else res
        return 404, "", None
    return AsyncMock(side_effect=get)


async def test_comparison_sites_hidden_source(hass: HomeAssistant, entry, no_network, hass_ws_client):
    c = await _setup(hass, entry)
    assert c.compare_enabled                                                     # on by default since 0.9.19
    hass.config_entries.async_update_entry(entry, options={**entry.options, "compare_sites": False})
    await hass.async_block_till_done()
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/compare/fetch"})
    assert (await ws.receive_json())["error"]["code"] == "not_enabled"          # switched off in Settings
    hass.config_entries.async_update_entry(entry, options={**entry.options, "compare_sites": True, "compare_sources": ["kieskeurig"]})
    await hass.async_block_till_done()
    c = hass.data[DOMAIN][entry.entry_id]
    assert c.compare_enabled
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "60454"}, blocking=True)
    await hass.services.async_call(DOMAIN, "set_offer", {"set_number": "60454", "retailer": "bol",
                                                         "url": "https://www.bol.com/nl/nl/p/lego-city-camper/9300000012345678/"}, blocking=True)
    no_network.side_effect = lambda rid, url, force=False: (None, "blocked (HTTP 403)")    # every shop blocks us
    page = _pages(**{"/search": (200, KK_SEARCH, None), "/product/": (200, KK_PRODUCT, None)})
    with patch.object(c.fetcher, "get_page", page):
        await c.refresh_all()
    urls = [a.args[1] for a in page.await_args_list]
    assert urls[0] == "https://www.kieskeurig.be/search?q=lego+60454" and "52114913" in urls[1]
    offers = c.store["offers"]["60454"]
    assert offers["bol"]["last_price"] == 24.99 and offers["bol"]["error"] is None           # bol.com via Kieskeurig
    assert offers["bol"]["last_via"] == "kieskeurig"                                         # shown as ⓒ
    assert offers["amazon_nl"]["via"] == "kieskeurig" and offers["amazon_nl"]["last_price"] == 27.49   # new shop via Kieskeurig
    assert c.store["sets"]["60454"]["ean"] == "5702017583723"
    check = [e for e in c.store["activity"] if e["kind"] == "check"][-1]
    assert check["results"]["bol"]["via"] == "kieskeurig" and check["results"]["bol"]["ok"]
    await ws.send_json({"id": 2, "type": "lego_tracker/set", "set_number": "60454"})
    card = (await ws.receive_json())["result"]
    assert len(card["compare"]["kieskeurig"]["shops"]) == 2 and set(card["compare"]) == {"kieskeurig"}
    n = page.await_count
    with patch.object(c.fetcher, "get_page", page):
        await c.refresh_set("60454")
    assert page.await_count == n                                                   # re-used for a few hours
    await ws.send_json({"id": 3, "type": "lego_tracker/overview"})
    ov = (await ws.receive_json())["result"]["compare"]
    assert ov["sources"]["kieskeurig"]["sets"] == 1 and "brickwatch" not in ov["sources"]
    # switching it through the settings stores the new option name
    await ws.send_json({"id": 4, "type": "lego_tracker/settings/set", "fields": {"compare_sites": False}})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    assert "brickwatch" not in entry.options and entry.options["compare_sites"] is False


async def test_builtin_catalogue_skips_lookups(hass: HomeAssistant, entry, no_network):
    import json

    from custom_components.lego_tracker import catalog

    cat = json.loads(catalog.PATH.read_text("utf-8"))["sets"]
    catalog._SETS = cat                                    # the real file (other tests run without it)
    assert len(cat) >= 100 and cat["10368"]["rrp"] == 29.99 and cat["10368"]["lego_url"].endswith("/product/10368")
    c = await _setup(hass, entry)
    lookup = AsyncMock(return_value=({"name": "from Brickset"}, "brickset.com"))
    with patch("custom_components.lego_tracker.coordinator.lookup_metadata", lookup):
        await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10368"}, blocking=True)
        await hass.services.async_call(DOMAIN, "add_set", {"set_number": "99999"}, blocking=True)
    s = c.store["sets"]["10368"]
    assert lookup.await_count == 1 and lookup.await_args.args[-1] == "99999"        # only the unknown set is looked up
    assert s["name"] == "Chrysanthemum" and s["rrp"] == 29.99 and s["rrp_source"] == "LEGO.com" and s["year"] == 2024
    assert s["image"].startswith("https://www.lego.com/") and s["availability"] == "retail" and s["exit_date"] == "2027-05-31"
    assert c.store["offers"]["10368"]["lego_com"]["url"] == "https://www.lego.com/nl-be/product/10368"   # no LEGO.com search
    assert not c.needs_enrich("10368") or not s.get("pieces")
    # your own values win: the catalogue only fills gaps
    c.store["sets"]["10368"].update(name="Mijn chrysant", name_source="user", rrp=25.0, rrp_source="user")
    catalog.apply("10368", c.store["sets"]["10368"], c.store["offers"]["10368"])
    assert s["name"] == "Mijn chrysant" and s["rrp"] == 25.0


async def test_removed_source_keeps_data_but_no_links(hass: HomeAssistant, entry, no_network, hass_storage):
    from custom_components.lego_tracker.models import new_store

    store = new_store()
    store["sets"]["10281"] = {"set_number": "10281", "name": "Bonsai Tree"}
    store["offers"]["10281"] = {
        "amazon_nl": {"url": "https://www.brickwatch.net/nl-BE/set/10281/", "via": "brickwatch", "link_status": "ok",
                      "history": [[1700000000, 37.1]], "last_price": 37.1},
        "bol": {"url": "https://www.bol.com/nl/nl/p/bonsai/9300000038297067/", "via": "brickwatch", "history": [[1700000000, 36.49]]}}
    store["brickwatch"] = {"10281": {"status": "ok", "ts": 1700000000, "url": "https://www.brickwatch.net/nl-BE/set/10281/",
                                     "shops": [{"name": "Top1Toys", "price": 41.0, "url": "https://www.brickwatch.net/nl-BE/go/9"}]}}
    hass_storage["lego_tracker.data"] = {"version": 1, "key": "lego_tracker.data", "data": store}
    c = await _setup(hass, entry)
    a, b = c.store["offers"]["10281"]["amazon_nl"], c.store["offers"]["10281"]["bol"]
    assert "url" not in a and a["history"] == [[1700000000, 37.1]] and a["last_price"] == 37.1   # data stays, link goes
    assert b["url"].startswith("https://www.bol.com/")                                           # a real shop link stays
    old = c.store["compare"]["brickwatch"]["10281"]
    assert "url" not in old and old["shops"] == [{"name": "Top1Toys", "price": 41.0}] and "brickwatch" not in c.store
    assert c.compare_entries("10281") == {}
    assert not any("brickwatch" in i["url"] for i in c.relay_items(100)["items"])


async def test_missing_set_not_retried_within_a_day(hass: HomeAssistant, entry, no_network):
    hass.config_entries.async_update_entry(entry, options={**entry.options, "compare_sites": True, "compare_sources": ["kieskeurig"]})
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "385"}, blocking=True)
    page = AsyncMock(return_value=(404, "", None))
    kk = lambda: c.store["compare"]["kieskeurig"]   # noqa: E731
    with patch.object(c.fetcher, "get_page", page):
        assert await c.compare_refresh("385") == {}
        assert await c.compare_refresh("385", force=True) == {}                        # not even forced
        assert page.await_count == 1 and kk()["385"]["status"] == "missing"
        kk()["385"]["ts"] -= 25 * 3600                                                 # a day later
        await c.compare_refresh("385")
        assert page.await_count == 2
    # a search page without this set counts as missing too
    empty = AsyncMock(return_value=(200, "<html><title>Zoeken</title><h1>Geen resultaten voor lego 385</h1></html>", None))
    kk().pop("385")
    with patch.object(c.fetcher, "get_page", empty):
        assert await c.compare_refresh("385") == {}
    assert kk()["385"]["status"] == "missing"
    assert "385" in c._spread_candidates()                                             # sets without links are checked too


KK_SEARCH = """<html><body><h1>Zoekresultaten lego 60454</h1><ul>
<li><a href="/bouw_en_constructiespeelgoed/product/1111-led-light-kit-for-lego-60454">LED Light Kit for LEGO 60454</a> vanaf € 19,99</li>
<li><a href="/bouw_en_constructiespeelgoed/product/52114913-lego-city-holiday-adventure-camper-van-60454">LEGO City 60454 Camper</a> vanaf € 24,99</li>
</ul></body></html>"""
KK_PRODUCT = """<html><head><title>LEGO City 60454 - Kieskeurig.be</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"LEGO City Vakantie camper 60454",
"gtin13":"5702017583723","offers":{"@type":"AggregateOffer","lowPrice":"24.99","offers":[
{"@type":"Offer","price":"24.99","priceCurrency":"EUR","seller":{"@type":"Organization","name":"bol.com"},"url":"https://www.kieskeurig.be/clickout/1"},
{"@type":"Offer","price":"27.49","priceCurrency":"EUR","seller":{"@type":"Organization","name":"Amazon.nl"},"url":"https://www.kieskeurig.be/clickout/2"},
{"@type":"Offer","price":"26.00","availability":"https://schema.org/OutOfStock","seller":{"name":"Fun"},"url":"https://www.kieskeurig.be/clickout/3"}]}}
</script></head><body><h1>LEGO City 60454</h1></body></html>"""
SHOPARIZE = """<html><body><script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"products":[
{"title":"LEGO Icons 40460 Rozen","price":{"amount":12.99},"shop":{"name":"Dreamland"},"clickoutUrl":"https://www.shoparize.com/go/a"},
{"title":"LEGO 40460 Rozen bouwset","price":"11,49","merchantName":"bol.com","url":"https://www.shoparize.com/go/b"},
{"title":"LED verlichting voor LEGO 40460","price":9.99,"merchantName":"LightMyBricks","url":"https://www.shoparize.com/go/c"},
{"title":"LEGO 40461 Tulpen","price":10.99,"merchantName":"bol.com","url":"https://www.shoparize.com/go/d"}]}}}</script></body></html>"""


def test_accessory_words_never_count_as_the_set():
    from custom_components.lego_tracker.parsers import title_check

    for t in ("LMB verlichtingsset voor LEGO 10368", "Lichtjes geschikt voor LEGO 10368", "LEGO 10368 lights kit",
              "Acrylglas vitrine LEGO 10368", "LEGO 10368 display case", "LED licht voor LEGO 10368"):
        assert title_check(t, "10368")[0] == "suspect", t
    for t, num in (("LEGO Icons Chrysant - Botanical Collection - 10368", "10368"),
                   ("LEGO City 60316 - geschikt voor kinderen vanaf 6 jaar", "60316")):
        assert title_check(t, num)[0] == "ok", t


async def test_own_filter_words_and_exceptions(hass: HomeAssistant, entry, no_network, hass_ws_client):
    from custom_components.lego_tracker import compare
    from custom_components.lego_tracker.parsers import title_check

    c = await _setup(hass, entry)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/settings/set",
                        "fields": {"block_words": "brickshine\nverlicht*", "allow_words": ["met lichtsteen", "Light brick"]}})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    c = hass.data[DOMAIN][entry.entry_id]
    await ws.send_json({"id": 2, "type": "lego_tracker/settings/get"})
    st = (await ws.receive_json())["result"]
    assert st["block_words"] == ["brickshine", "verlicht*"] and st["allow_words"] == ["met lichtsteen", "Light brick"]
    assert "LMB" in st["builtin_words"]
    # own words work everywhere: link check and comparison sites
    assert title_check("Brickshine kit LEGO 10368", "10368")[0] == "suspect"
    assert title_check("LEGO 10368 verlichtbaar model", "10368")[0] == "suspect"            # verlicht* = any ending
    assert compare.is_accessory("BrickShine 10368")
    # exceptions: a real set with a light brick is the set again
    assert title_check("LEGO Icons 10368 met lichtsteen", "10368")[0] == "ok"
    assert title_check("LEGO 10368 with light brick", "10368")[0] == "ok"
    assert title_check("LEGO 10368 licht", "10368")[0] == "suspect"                          # the built-in word still counts
    # try a title with unsaved lists (the saved ones stay in force)
    await ws.send_json({"id": 3, "type": "lego_tracker/filter/test", "title": "Brickshine 10368", "block_words": [], "allow_words": []})
    assert (await ws.receive_json())["result"]["word"] is None
    await ws.send_json({"id": 4, "type": "lego_tracker/filter/test", "title": "Brickshine 10368"})
    assert (await ws.receive_json())["result"]["word"].lower() == "brickshine"
    await ws.send_json({"id": 5, "type": "lego_tracker/settings/set", "fields": {"block_words": ["a*b"]}})
    assert not (await ws.receive_json())["success"]                                          # * only at the end


def test_compare_parsers_skip_accessories_and_follow():
    from custom_components.lego_tracker import compare
    from custom_components.lego_tracker.shops import all_domains

    d = all_domains()
    # Kieskeurig: search -> product page (not the LED kit), JSON-LD offers, out of stock skipped, EAN kept
    r = compare.parse("kieskeurig", KK_SEARCH, "60454", "https://www.kieskeurig.be/search?q=lego+60454", d)
    assert r.kind == "follow" and "52114913" in r.url
    r = compare.parse("kieskeurig", KK_PRODUCT, "60454", r.url, d, step=1)
    assert [(x["retailer"], x["price"]) for x in r.shops] == [("bol", 24.99), ("amazon_nl", 27.49)] and r.ean == "5702017583723"
    # Shoparize: embedded JSON search results; LED kits and other sets are dropped
    r = compare.parse("shoparize", SHOPARIZE, "40460", "https://www.shoparize.com/be/q?q=lego+40460", d)
    assert [(x["name"], x["price"]) for x in r.shops] == [("bol.com", 11.49), ("Dreamland", 12.99)] and r.name is None
    # Channable renders its results with JavaScript: the HTML doesn't even contain the query -> 'unreadable', not 'missing'
    js_page = '<html><head><title>Search and Compare Prices Across Webshops | Channable</title></head><body><div id="__next"></div>' \
              '<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"page":{"title":"x"}}},"page":"/","query":{}}</script></body></html>'
    r = compare.parse("channable", js_page, "10368", "https://shopping.channable.com/?country=BE&search=lego+10368", d)
    assert r.kind == "missing" and r.note == "js"
    # accessories anywhere on a page are skipped; the next row with the set number counts
    mixed = """<html><body><h1>Zoekresultaten lego 10368</h1>
    <div class="card"><a href="https://shopx.example/lmb">LMB verlichtingsset voor LEGO 10368</a> <b>€ 19,99</b> <span class="shop">ShopX</span></div>
    <div class="card"><a href="https://shopy.example/acryl">Acryl display vitrine geschikt voor LEGO 10368</a> <b>€ 24,99</b> <span class="shop">ShopY</span></div>
    <div class="card"><a href="https://shopz.example/lights">BrickBling lights 10368</a> <b>€ 14,99</b> <span class="shop">ShopZ</span></div>
    <div class="card"><a href="https://www.dreamland.be/e/lego-10368">LEGO Icons 10368 Chrysant</a> <b>€ 27,99</b> <span class="shop">Dreamland</span></div>
    </body></html>"""
    r = compare.parse("shoparize", mixed, "10368", "https://www.shoparize.com/be/q?q=lego+10368", d)
    assert [(x["retailer"], x["price"]) for x in r.shops] == [("dreamland_be", 27.99)]
    # Producthero needs the EAN
    assert compare.first_url("producthero", "60454", "nl-be") is None
    assert compare.first_url("producthero", "60454", "nl-be", "5702016914177") == \
        "https://shopping.producthero.com/nl/product/05702016914177?country=be"
    assert compare.first_url("channable", "10328", "nl-be") == "https://shopping.channable.com/?country=BE&search=lego+10328"


async def test_compare_network_errors_pause_one_hour_and_job_stops(hass: HomeAssistant, entry, no_network):
    hass.config_entries.async_update_entry(entry, options={**entry.options, "compare_sites": True, "compare_sources": ["kieskeurig"]})
    c = await _setup(hass, entry)
    for n in ("10281", "10300", "10305", "10311", "10313", "10316", "10317"):
        await hass.services.async_call(DOMAIN, "add_set", {"set_number": n}, blocking=True)
    err = "network error: Failed to perform, curl: (35) BoringSSL SSL_connect: Connection closed abruptly"
    page = AsyncMock(return_value=(0, "", err))
    with patch.object(c.fetcher, "get_page", page), \
            patch("homeassistant.helpers.event.async_call_later") as later:
        c.start_compare()
        await c._job_task
    assert page.await_count == 5                                   # 5 network errors in a row: stop asking
    assert 3500 < c.fetcher.cooldown_left("kieskeurig") <= 3600
    assert c.last_job["cancelled"] and later.call_args.args[1] > 3600    # the rest follows after the pause
    assert any("5 network errors in a row" in e["message"] for e in c.store["activity"])
    with patch.object(c.fetcher, "get_page", page):
        await c.compare_refresh("10281")                           # paused: skipped
    assert page.await_count == 5


# structure of a real Kieskeurig.be search result (2026-09): click-outs via ocean.kieskeurig.be, prices in 'font-bold'
KK_CARDS = """<html><body><ul class="productlist_grid">
<li><article class="productcard"><a href="/bouw_en_constructiespeelgoed/product/51044251-lego-icons-chrysant-botanical-collection-10368">LEGO Icons Chrysant - Botanical Collection - 10368</a>
<span>v.a. € 15,98</span><a href="https://ocean.kieskeurig.be/e/c/aaa" class="productcard_cta" rel="sponsored nofollow noopener">Naar goedkoopste shop</a>
<ul class="productcard_pricelist"><li><a href="https://ocean.kieskeurig.be/e/c/bbb" class="productcard_priceitem-link" rel="sponsored nofollow noopener">
<span class="productcard_priceitem-shop">bol.</span><div><span class="productcard_priceitem-amount font-bold">€ 15,98</span></div></a></li>
<li><a href="https://ocean.kieskeurig.be/e/c/ccc" class="productcard_priceitem-link" rel="sponsored nofollow noopener">
<span class="productcard_priceitem-shop">Wehkamp</span><div><span class="productcard_priceitem-amount font-bold">€ 27,89</span></div></a></li></ul></article></li>
<li><article class="productcard"><a href="/bouw_en_constructiespeelgoed/product/51370743-lego-botanical-collection-10369">LEGO 10369</a>
<ul><li><a href="https://ocean.kieskeurig.be/e/c/ddd" rel="sponsored"><span class="productcard_priceitem-shop">bol.</span><span class="font-bold">€ 39,99</span></a></li></ul></article></li>
</ul></body></html>"""


async def test_kieskeurig_product_page_403_uses_search_results(hass: HomeAssistant, entry, no_network):
    hass.config_entries.async_update_entry(entry, options={**entry.options, "compare_sites": True, "compare_sources": ["kieskeurig"]})
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10368"}, blocking=True)

    async def get(src, url, force=False, note_block=True):
        if "/search" in url:
            return 200, KK_CARDS, None
        return 403, "", "blocked (HTTP 403)"
    page = AsyncMock(side_effect=get)
    with patch.object(c.fetcher, "get_page", page):
        got = await c.compare_refresh("10368")
    assert [(x["retailer"], x["price"]) for x in got["kieskeurig"]["shops"]] == [("bol", 15.98), (None, 27.89)]
    assert page.await_args_list[1].kwargs["note_block"] is False and c.fetcher.cooldown_left("kieskeurig") == 0   # site not paused
    with patch.object(c.fetcher, "get_page", page):
        await c.compare_refresh("10368", refresh=True)
    assert page.await_count == 3                     # product pages skipped for a day: only the search page


# structure of a real BrickEconomy set page (2026-09, region Europe)
BE_PAGE = """<html><head><script type="application/ld+json">{"@context": "https://schema.org/","@type": "Product",
"name": "LEGO Botanical Collection Chrysanthemum","image": ["https://www.brickeconomy.com/resources/images/sets/lego-10368-1_xlarge.jpg"],
"sku": "10368-1","gtin13": "5702017719689","offers": {"@type": "AggregateOffer","lowPrice": "23.79","priceCurrency": "USD"}}</script></head>
<body><div>Theme</div><div>Botanicals</div><div>Year</div><div>2025</div>
<h4>Set Details</h4><div>Set number</div><div>10368-1</div><div>Name</div><div>Chrysanthemum</div><div>Theme</div><div><a>Icons</a></div>
<div>Subtheme</div><div>Botanical Collection</div><div>Year</div><div>2024</div><div>Availability</div><div>Retail</div>
<div>Pieces</div><div>278 <small>(PPP €0.11)</small></div>
<h4>Set Pricing</h4><div>Retail price</div><div>€29.99</div><div>Market price</div><div>€22.31</div><div>-25.6%</div>
<h4>Set Predictions</h4><div>Retirement</div><div>Early to mid 2027</div><div>69.6%</div><div>1 year retired</div><div>€35.07</div>
<div>5 years retired</div><div>€33 - €37</div><div>EAN</div><div>5702017719689</div></body></html>"""


async def test_brickeconomy_market_value_and_retirement(hass: HomeAssistant, entry, no_network):
    from custom_components.lego_tracker import compare

    r = compare.parse("brickeconomy", BE_PAGE, "10368", "https://www.brickeconomy.com/set/10368-1/", {})
    assert r.kind == "data" and r.data["theme"] == "Icons" and r.data["year"] == 2024 and r.data["pieces"] == 278
    assert r.data["market_new"] == 22.31 and r.rrp == 29.99 and r.data["forecast_5y"] == [33.0, 37.0] and r.ean == "5702017719689"
    assert compare.forecast_date("Early to mid 2027") == "2027-05-31" and compare.forecast_date("Retired December 2024") == "2024-12-31"
    assert compare.parse("brickeconomy", BE_PAGE.replace("10368-1</div>", "10369-1</div>").replace("10368", "x"), "10368", "u", {}).kind == "missing"
    assert compare._eur("$24.99") is None                                  # only euro values

    hass.config_entries.async_update_entry(entry, options={**entry.options, "compare_sites": True, "compare_sources": ["brickeconomy"]})
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10368"}, blocking=True)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281"}, blocking=True)
    c.store["collection"]["10368"] = {"qty": 1, "paid": 25.0, "condition": "Sealed"}
    c.store["sets"]["10281"].update(exit_date="2026-12-31", exit_date_source="user")
    page = AsyncMock(side_effect=lambda src, url, force=False, note_block=True:
                     (200, BE_PAGE if "10368" in url else BE_PAGE.replace("10368", "10281"), None))
    with patch.object(c.fetcher, "get_page", page):
        await c.compare_refresh("10368")
        await c.compare_refresh("10281")
    assert page.await_args_list[0].args[1] == "https://www.brickeconomy.com/set/10368-1/"
    s, e = c.store["sets"]["10368"], c.store["collection"]["10368"]
    assert s["exit_date"] == "2027-05-31" and s["exit_date_source"] == "Market value" and s["market"]["market_new"] == 22.31
    assert e["current_value"] == 22.31 and e["value_source"] == "Market value" and len(e["value_history"]) == 1
    assert c.store["sets"]["10281"]["exit_date"] == "2026-12-31"           # your own date wins
    assert c.compare_prices("10368") == {}                                  # no shop prices from BrickEconomy
    c.store["compare"]["brickeconomy"]["10368"]["ts"] -= 12 * 3600
    with patch.object(c.fetcher, "get_page", page):
        await c.compare_refresh("10368")
    assert page.await_count == 2                                            # re-used for a day, not 6 h


async def test_relay_fetches_comparison_pages(hass: HomeAssistant, entry, no_network, hass_client):
    hass.config_entries.async_update_entry(entry, options={**entry.options, "compare_sites": True, "compare_sources": ["kieskeurig"]})
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "60454"}, blocking=True)
    c.fetcher.blocked_until["kieskeurig"] = time.time() + 3600       # the server is refused
    client = await hass_client()
    items = (await (await client.get("/api/lego_tracker/relay")).json())["items"]
    it = next(i for i in items if i.get("kind") == "page")
    assert it["url"] == "https://www.kieskeurig.be/search?q=lego+60454" and it["step"] == 0
    r = await (await client.post("/api/lego_tracker/relay", json={"results": [{**it, "status": 200, "html": KK_SEARCH}]})).json()
    nxt = r["follow"][0]
    assert "52114913" in nxt["url"] and nxt["step"] == 1
    r = await (await client.post("/api/lego_tracker/relay", json={"results": [{**nxt, "status": 200, "html": KK_PRODUCT}]})).json()
    assert r["ok"] == 1
    e = c.store["compare"]["kieskeurig"]["60454"]
    assert e["status"] == "ok" and e["via"] == "relay" and c.store["sets"]["60454"]["ean"] == "5702017583723"
    # only pages of comparison sites for tracked sets
    r = await (await client.post("/api/lego_tracker/relay", json={"results": [{**it, "url": "https://evil.example/", "status": 200, "html": ""}]})).json()
    assert r["rejected"]


@pytest.mark.parametrize("source,url", [
    ("shoparize", "https://www.shoparize.com/be/q?q=lego+60454"),
    ("channable", "https://shopping.channable.com/?search=lego+60454"),
    ("producthero", "https://shopping.producthero.com/nl/product/123"),
])
async def test_relay_comparison_page_with_many_priceless_links(hass: HomeAssistant, entry, no_network, hass_client, source, url):
    hass.config_entries.async_update_entry(entry, options={**entry.options, "compare_sites": True, "compare_sources": [source]})
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "60454"}, blocking=True)
    page = '<div>' + '<a href="https://noise.example/item">LEGO 60454</a>' * 2000 + '</div>'
    page += '<div><a href="https://www.dreamland.be/lego-60454">LEGO 60454</a><b>€ 27,99</b><span class="shop">Dreamland</span></div>'
    client = await hass_client()
    response = await client.post("/api/lego_tracker/relay", json={"results": [{
        "kind": "page", "source": source, "set_number": "60454", "url": url, "status": 200, "html": page,
    }]})
    assert response.status == 200
    result = await response.json()
    assert result["ok"] == 1 and not result["rejected"]
    shops = c.store["compare"][source]["60454"]["shops"]
    assert [(shop["retailer"], shop["price"]) for shop in shops] == [("dreamland_be", 27.99)]


async def test_find_uses_pasted_search_page_and_says_why(hass: HomeAssistant, entry, no_network):
    from custom_components.lego_tracker.parsers import _generic_result

    tile = """<div class="grid"><div class="tile"><a href="/be/nl-be/speelgoed/lego/p/192811"><img alt="LEGO City Brandweerkazerne"></a>
      <span>Artikelnummer: 60510</span><span>€ 49,99</span></div>
      <div class="tile"><a href="/be/nl-be/speelgoed/lego/p/200001">LED-verlichting voor LEGO 60510</a></div></div>"""
    assert _generic_result(tile, "smythstoys.com", "60510") == "https://www.smythstoys.com/be/nl-be/speelgoed/lego/p/192811"
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "60510"}, blocking=True)
    # a search page pasted by hand is searched, not saved as the link
    seen = []

    async def discover(retailer, num, force=False, url=None):
        seen.append(url)
        c.fetcher.discover_error[retailer] = "the shop blocked the search (HTTP 403)"
        return None
    with patch.object(c.fetcher, "discover", discover):
        r = await c.fetch_shop("60510", "bol", "https://www.bol.com/nl/nl/s/?searchtext=60510")
    assert seen == ["https://www.bol.com/nl/nl/s/?searchtext=60510"]
    assert r["error"] == "the shop blocked the search (HTTP 403)"                 # the real reason, not "no product"
    assert "bol" not in c.store["offers"]["60510"]
    with pytest.raises(ValueError, match="search page"):
        c.set_offer("60510", "bol", "https://www.bol.com/nl/nl/s/?searchtext=60510")   # never saved as a product link
    # a product page pasted by hand becomes the link
    await c.fetch_shop("60510", "bol", "https://www.bol.com/nl/nl/p/lego-city-60510/9300000012345678/")
    assert c.store["offers"]["60510"]["bol"]["url"].startswith("https://www.bol.com/nl/nl/p/")


async def test_full_refresh_off_by_default_and_once_a_minute(hass: HomeAssistant, entry, no_network, hass_ws_client):
    opts = {k: v for k, v in entry.options.items() if not k.startswith("dev_")}
    hass.config_entries.async_update_entry(entry, options={**opts, "refresh_mode": "times", "spread_hours": 5})
    c = await _setup(hass, entry)
    assert c.refresh_mode == "spread"                        # fixed times only in developer mode
    assert c.spread_hours == 6                               # nearest allowed cycle (2/3/4/6/12/24)
    with pytest.raises(ServiceValidationError, match="Switched off"):
        await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True, return_response=True)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/settings/set", "fields": {"spread_hours": 5}})
    assert not (await ws.receive_json())["success"]          # only the choices
    await ws.send_json({"id": 2, "type": "lego_tracker/settings/set", "fields": {"refresh_mode": "times"}})
    assert not (await ws.receive_json())["success"]
    await ws.send_json({"id": 3, "type": "lego_tracker/settings/set", "fields": {"spread_hours": 12, "watch_cycle_min": 30}})
    assert (await ws.receive_json())["success"]
    await hass.async_block_till_done()
    # developer mode: a full round is possible, but at most once a minute
    hass.config_entries.async_update_entry(entry, options={**entry.options, "dev_full_refresh": True})
    await hass.async_block_till_done()
    c = hass.data[DOMAIN][entry.entry_id]
    with patch("custom_components.lego_tracker.coordinator.FULL_REFRESH_GAP", 60):
        c.start_full_refresh()
        await c._job_task
        with pytest.raises(ValueError, match="once a minute"):
            c.start_full_refresh()


async def test_watchlist_cycle_and_limit(hass: HomeAssistant, entry, no_network):
    hass.config_entries.async_update_entry(entry, options={**entry.options, "dev_free_cycle": False, "spread_hours": 24,
                                                           "watch_cycle_min": 60})
    c = await _setup(hass, entry)
    for n in ("10281", "42143", "21028", "10300"):
        await hass.services.async_call(DOMAIN, "add_set", {"set_number": n}, blocking=True)
        await hass.services.async_call(DOMAIN, "set_offer", {"set_number": n, "retailer": "bol",
                                                             "url": f"https://www.bol.com/nl/nl/p/x/{n}00/"}, blocking=True)
    c.store["collection"]["10300"] = {"qty": 1}              # 3 on the watchlist, 1 owned
    rates = c.check_rates()
    assert rates["watched"] == 3 and rates["watch"] == 3.0 and abs(rates["main"] - 1 / 24) < 1e-9
    assert abs(c.spread_interval() - 3600 / (3 + 1 / 24)) < 1e-6
    now = time.time()
    for n, age in (("10281", 30), ("42143", 90), ("21028", 70), ("10300", 3000)):
        c.store["sets"][n]["checked"] = now - age * 60
    assert c.next_spread_set() == "42143"                     # due watchlist set first (oldest)
    c.store["sets"]["42143"]["checked"] = c.store["sets"]["21028"]["checked"] = now
    assert c.next_spread_set() == "10300"                     # then the normal cycle
    # the watchlist holds at most 100 sets (owned sets don't count)
    with patch("custom_components.lego_tracker.coordinator.WATCH_LIMIT", 3):
        hass.config_entries.async_update_entry(entry, options={**entry.options})
        with pytest.raises(ServiceValidationError, match="full"):
            await hass.services.async_call(DOMAIN, "add_set", {"set_number": "75192"}, blocking=True)
        await hass.services.async_call(DOMAIN, "add_set", {"set_number": "75192", "owned": True}, blocking=True)


async def test_logbook_export(hass: HomeAssistant, entry, no_network, hass_ws_client):
    import csv
    import io

    c = await _setup(hass, entry)
    for n in ("10281", "42143"):
        await hass.services.async_call(DOMAIN, "add_set", {"set_number": n, "theme": "Icons" if n == "10281" else "Technic"}, blocking=True)
        await hass.services.async_call(DOMAIN, "set_offer", {"set_number": n, "retailer": "bol",
                                                             "url": f"https://www.bol.com/nl/nl/p/x/{n}00/"}, blocking=True)
    c.store["collection"]["42143"] = {"qty": 1, "condition": "Sealed", "location": "Zolder kast 2"}
    now = time.time()
    c.store["offers"]["10281"]["bol"]["history"] = [[now - 3 * 86400, 40.0], [now - 86400, 36.0]]
    c.store["offers"]["42143"]["bol"]["history"] = [[now - 2 * 86400, 300.0]]
    await c.refresh_set("10281")                                       # one check entry (bol: ok)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/logs/export", "parts": ["checks"], "filters": {"shops": ["bol"], "status": "ok"}})
    r = (await ws.receive_json())["result"]
    rows = list(csv.DictReader(io.StringIO(r["csv"])))
    assert r["counts"]["checks"] >= 1 and all(x["shop"] == "bol.com" and x["result"] == "ok" for x in rows)
    # price history of the collection only (location filter), in a period
    await ws.send_json({"id": 2, "type": "lego_tracker/logs/export", "parts": ["history"], "start": now - 5 * 86400,
                        "filters": {"scope": "collection", "location": "kast 2"}})
    rows = list(csv.DictReader(io.StringIO((await ws.receive_json())["result"]["csv"])))
    assert {x["set_number"] for x in rows} == {"42143"} and rows[0]["price"] == "300.0"
    await ws.send_json({"id": 3, "type": "lego_tracker/logs/export", "parts": ["history"], "filters": {"theme": "Icons", "scope": "watchlist"}})
    rows = list(csv.DictReader(io.StringIO((await ws.receive_json())["result"]["csv"])))
    assert [x["price"] for x in rows if x["set_number"] == "10281"][:2] == ["40.0", "36.0"]
    # total per day over several parts: one file with a table per part
    await ws.send_json({"id": 4, "type": "lego_tracker/logs/export", "parts": ["total", "checks"], "start": now - 4 * 86400})
    r = (await ws.receive_json())["result"]
    assert r["csv"].startswith("# Shop checks") and "# Price history in total" in r["csv"] and r["counts"]["total"] >= 3
    total = r["csv"].split("# Price history in total\n")[1]
    last = list(csv.DictReader(io.StringIO(total)))[-1]
    assert last["sets_with_price"] == "2"



async def test_pasted_search_page_on_existing_link_keeps_state(hass: HomeAssistant, entry, no_network):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "60510"}, blocking=True)
    old = "https://www.bol.com/nl/nl/p/lego-city-60510/9300000012345678/"
    c.update_offer("60510", "bol", url=old, manual_price="39.99")
    c.store["offers"]["60510"]["bol"]["history"] = [[1700000000, 45.0]]
    seen = []

    async def discover(retailer, num, force=False, url=None):
        seen.append(url)
        return old                                    # the search page finds the same product
    with patch.object(c.fetcher, "discover", discover):
        await c.fetch_shop("60510", "bol", "https://www.bol.com/nl/nl/s/?searchtext=60510")
    o = c.store["offers"]["60510"]["bol"]
    assert seen == ["https://www.bol.com/nl/nl/s/?searchtext=60510"]      # searched although a link existed
    assert o["manual_price"]["price"] == 39.99 and o["history"][0] == [1700000000, 45.0]
    # a pasted product page on an existing link: same page keeps its history, the manual price stays
    await c.fetch_shop("60510", "bol", old + "?ref=x")
    o = c.store["offers"]["60510"]["bol"]
    assert o["history"][0] == [1700000000, 45.0] and o["manual_price"]["price"] == 39.99
    assert o["manual_url"] and o["link_status"] == "confirmed"
    # a search that finds the same page: history and manual price stay, it is no longer "set by hand"
    with patch.object(c.fetcher, "discover", discover):
        await c.fetch_shop("60510", "bol", "https://www.bol.com/nl/nl/s/?searchtext=60510")
    o = c.store["offers"]["60510"]["bol"]
    assert o["history"][0] == [1700000000, 45.0] and o["manual_price"]["price"] == 39.99
    assert not o.get("manual_url") and o.get("link_reason") != "set by hand"
    # a search that finds another page: new history, the manual price stays, not confirmed by hand
    other = "https://www.bol.com/nl/nl/p/lego-city-60510-b/9300000099999999/"

    async def discover_other(retailer, num, force=False, url=None):
        return other
    with patch.object(c.fetcher, "discover", discover_other):
        await c.fetch_shop("60510", "bol", "https://www.bol.com/nl/nl/s/?searchtext=60510")
    o = c.store["offers"]["60510"]["bol"]
    assert o["url"] == other and not any(ts == 1700000000 for ts, _ in o["history"])
    assert o["manual_price"]["price"] == 39.99 and not o.get("manual_url")


def test_tile_search_never_mixes_products_or_accepts_knockoffs():
    from custom_components.lego_tracker.parsers import _generic_result

    # the review's case: two tiles, only the second names the set -> never the first tile's link
    two = """<div class="grid">
      <div class="tile"><a href="/be/p/111"><img alt="Brandweerauto"></a></div>
      <div class="tile"><a href="/be/p/222"><img alt="LEGO City"></a><span>Artikel 60510</span></div></div>"""
    assert _generic_result(two, "smythstoys.com", "60510") == "https://www.smythstoys.com/be/p/222"
    # a cart / wishlist link inside the right tile is not another product
    nav = two.replace("<span>Artikel 60510</span>", '<a href="/be/cart/add/222">In winkelmandje</a><span>Artikel 60510</span>')
    assert _generic_result(nav, "smythstoys.com", "60510") == "https://www.smythstoys.com/be/p/222"
    # a neighbouring product whose slug contains a navigation word is still another product
    slug = """<div class="grid"><div><a href="/products/lego-222"><img alt="LEGO City"></a></div>
      <div><a href="/products/cart-111"><img alt="Bolderkar"></a></div><span>Artikel 60510</span></div>"""
    assert _generic_result(slug, "smythstoys.com", "60510") is None      # the number is not in lego-222's own tile
    grid = """<div class="grid"><div><a href="/products/lego-222"><img alt="LEGO City"></a></div>
      <div><a href="/products/review-kit-333"><img alt="Review kit"></a></div><span>Artikel 60510</span></div>"""
    assert _generic_result(grid, "smythstoys.com", "60510") is None      # the number sits outside both tiles
    knock = """<div class="tile"><a href="/be/p/333"><img alt="Bouwset"></a><span>Mould King compatible with LEGO 60510</span></div>"""
    assert _generic_result(knock, "smythstoys.com", "60510") is None


async def test_logbook_export_rejects_bad_priority(hass: HomeAssistant, entry, no_network, hass_ws_client):
    await _setup(hass, entry)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/logs/export", "parts": ["history"], "filters": {"priority": "high"}})
    r = await ws.receive_json()
    assert not r["success"] and r["error"]["code"] == "invalid"
    for i, prio in enumerate((2.5, "1.5", True, 4), start=2):
        await ws.send_json({"id": i, "type": "lego_tracker/logs/export", "parts": ["history"], "filters": {"priority": prio}})
        r = await ws.receive_json()
        assert not r["success"] and r["error"]["code"] == "invalid", prio
    await ws.send_json({"id": 9, "type": "lego_tracker/logs/export", "parts": ["history"], "filters": {"priority": "2.0"}})
    assert (await ws.receive_json())["success"]


def test_producthero_reads_inertia_shop_prices():
    import json as _json

    from custom_components.lego_tracker import compare

    def shop(name, price, sale, cur="EUR", title="LEGO Icons Chrysant 10368"):
        return {"title": name, "product_price": price, "product_sale_price": sale, "currency_code_google": cur,
                "product_title": title, "checkout_link": f"https://shopping.producthero.com/nl/clickout?stitle={name}"}
    data = {"component": "Shopping/Product", "props": {"product": {"data": {
        "eancode": "05702017719689", "title": "LEGO Botanical Collection Chrysant 10368", "images": ["https://img/x.jpg"],
        "shops": [shop("coolblue.be", 27.99, 0), shop("Wehkamp", 29.99, 23.99), shop("carturesti.ro", 159.99, 0, "RON"),
                  shop("lampjes.nl", 19.99, 0, title="LED verlichting voor LEGO 10368")]}}}}
    page = ('<html><title>Producthero Shopping</title><div id="app"></div>'
            f'<script data-page="app" type="application/json">{_json.dumps(data)}</script></html>')
    r = compare.parse("producthero", page, "10368", "https://shopping.producthero.com/nl/product/05702017719689",
                      {"coolblue": "coolblue.be"})
    assert r.kind == "offers" and r.ean == "5702017719689" and "Chrysant" in r.name
    assert [(s["name"], s["price"]) for s in r.shops] == [("Wehkamp", 23.99), ("coolblue.be", 27.99)]   # sale price; no RON, no LED kit
    assert r.shops[1]["retailer"] == "coolblue"
    other = page.replace("10368", "10369")
    assert compare.parse("producthero", other, "10368", "https://shopping.producthero.com/nl/product/1", {}).kind == "missing"


async def test_watch_on_and_off_for_owned_and_not_owned_sets(hass: HomeAssistant, entry, no_network):
    c = await _setup(hass, entry)
    for n in ("10281", "10311"):
        await hass.services.async_call(DOMAIN, "add_set", {"set_number": n}, blocking=True)
    c.update_set("10311", {"owned": True})
    assert c.is_watched("10281") and not c.is_watched("10311")
    assert c.compute()["wishlist"]["sets"] == 1
    c.update_set("10311", {"watch": True})               # owned, and on the watchlist too: counted
    assert c.is_watched("10311") and c.compute()["wishlist"]["sets"] == 2
    c.update_set("10281", {"watch": False})              # −W on a set you don't own
    assert not c.is_watched("10281") and c.compute()["wishlist"]["sets"] == 1
    assert "10281" not in c.watched_sets()
    c.update_set("10281", {"watch": None})               # back to the default: not owned = watched
    assert c.is_watched("10281")
    c.update_set("10281", {"watch": False})
    await c.add_set("10281", discover=False)             # added to the watchlist again
    assert c.is_watched("10281")


async def test_problem_report_to_logbook_and_csv(hass: HomeAssistant, entry, no_network, hass_ws_client):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281"}, blocking=True)
    c.update_offer("10281", "bol", url="https://www.bol.com/nl/nl/p/lego-bonsai-10281/9300000012345678/")
    c.store["offers"]["10281"]["bol"].update(error="price not found on the page", history=[[1700000000, 41.5]])
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/report", "set_number": "10281", "problems": ["price", "link", "bogus"],
                        "shops": ["bol"], "comment": "price is from an LED kit", "save": False})
    r = await ws.receive_json()
    assert r["success"] and not c.store.get("reports")                       # CSV only: nothing stored
    csv = r["result"]["csv"]
    assert "# Problem reports: shops" in csv and "price is from an LED kit" in csv and "price not found on the page" in csv
    assert "bol.com" in csv and "=41.5" in csv and "bogus" not in csv
    await ws.send_json({"id": 2, "type": "lego_tracker/report", "set_number": "10281", "problems": ["price"], "comment": "x"})
    r = await ws.receive_json()
    assert r["success"] and len(c.store["reports"]) == 1
    entry_ = c.store["activity"][-1]
    assert entry_["kind"] == "report" and entry_["level"] == "warning" and entry_["report"] == r["result"]["id"]
    await ws.send_json({"id": 3, "type": "lego_tracker/logs/export", "parts": ["reports"]})
    r = await ws.receive_json()
    assert r["success"] and r["result"]["counts"] == {"reports": 1} and "# Problem reports: recent log" in r["result"]["csv"]


async def test_manual_actions_wait_two_minutes(hass: HomeAssistant, entry, no_network, hass_ws_client):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281"}, blocking=True)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10311"}, blocking=True)
    c.update_offer("10281", "bol", url="https://www.bol.com/nl/nl/p/lego-bonsai-10281/9300000012345678/")
    with patch("custom_components.lego_tracker.coordinator.MANUAL_GAP", 120):
        await hass.services.async_call(DOMAIN, "refresh", {"set_number": "10281"}, blocking=True, return_response=True)
        with pytest.raises(ServiceValidationError, match="once every 2 minutes"):      # also for another set
            await hass.services.async_call(DOMAIN, "refresh", {"set_number": "10311"}, blocking=True, return_response=True)
        st = c.manual_status()
        assert st["prices"] and "bol" in st["shops"] and st["find"] is None
        with pytest.raises(ValueError, match="once every 2 minutes"):                  # the shop's site was just asked
            await c.fetch_shop("10311", "bol")
        await c.fetch_shop("10311", "amazon_de")                                         # another site is fine
        with pytest.raises(ValueError):
            await c.fetch_shop("10281", "amazon_de")
        ws = await hass_ws_client(hass)
        await ws.send_json({"id": 1, "type": "lego_tracker/overview"})
        r = await ws.receive_json()
        assert r["result"]["manual"]["shops"].keys() >= {"bol", "amazon_de"}


async def test_fetcher_spaces_requests_per_site():
    from custom_components.lego_tracker.client import Fetcher

    f = Fetcher(None, use_impersonation=False)
    f.last_request["bol.com"] = time.time()
    assert 25 < f.next_free("https://www.bol.com/nl/nl/p/x/") <= 30
    assert f.next_free("https://www.amazon.nl/dp/B0") == 0
    f.last_search["bol.com"] = time.time()
    assert 115 < f.next_free("bol.com", search=True) <= 120


async def test_new_custom_shop_is_used_and_shop_detail(hass: HomeAssistant, entry, no_network, hass_ws_client):
    c = await _setup(hass, entry)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/settings/set", "fields": {
        "custom_shops": [{"id": "c_smyths", "name": "Smyths", "domain": "smythstoys.com",
                          "search": "https://www.smythstoys.com/be/nl-be/search?text={query}"}]}})
    assert (await ws.receive_json())["result"]["saved"]
    await hass.async_block_till_done()
    c = hass.data[DOMAIN][entry.entry_id]
    assert "c_smyths" in c.retailers                          # added shops are searched and fetched right away
    c.fetcher._trace("c_smyths", "search", "https://www.smythstoys.com/be/nl-be/search?text=LEGO+10368", time.time(), 200, 51234,
                     None, "the search page does not contain 10368: this shop probably loads its results with JavaScript.", "10368")
    await ws.send_json({"id": 2, "type": "lego_tracker/shop/detail", "retailer": "c_smyths"})
    d = (await ws.receive_json())["result"]
    assert d["site"] == "smythstoys.com" and d["trace"][0]["status"] == 200 and d["enabled"]
    assert any("JavaScript" in h for h in d["hints"])


async def test_continuous_check_priorities_search_and_via(hass: HomeAssistant, entry, no_network, hass_client):
    c = await _setup(hass, entry)
    for n in ("10281", "10311", "42143"):
        await hass.services.async_call(DOMAIN, "add_set", {"set_number": n}, blocking=True)
    offers = c.store["offers"]
    offers["10281"]["bol"] = {"url": "https://www.bol.com/nl/nl/p/lego-bonsai-10281/9300000012345678/", "history": []}
    offers["10311"]["bol"] = {"url": "https://www.bol.com/nl/nl/p/lego-orchidee-10311/9300000012345679/", "history": [[1, 39.99]],
                              "last_ok": 1, "error": "blocked (HTTP 403)", "available": False}
    offers["42143"]["lego_com"] = {"url": "https://www.lego.com/nl-be/product/42143", "history": [[1, 449.99]],
                                   "last_ok": time.time(), "available": True, "last_price": 449.99}
    q = c.continuous_items(50)
    reasons = [(i["reason"], i["set_number"], i["retailer"]) for i in q["items"]]
    assert reasons[0] == ("no_price", "10281", "bol")                         # never a price: first
    first_search = next(k for k, (r, *_x) in enumerate(reasons) if r == "search")
    assert reasons.index(("open_error", "10311", "bol")) < first_search        # open errors before searches
    offers["10311"]["bol"]["ignored_error"] = "blocked (HTTP 403)"               # ignored: only as a failing link, last
    reasons = [(i["reason"], i["set_number"], i["retailer"]) for i in c.continuous_items(50)["items"]]
    assert reasons.index(("server_fails", "10311", "bol")) > first_search
    del offers["10311"]["bol"]["ignored_error"]
    assert not any(n == "42143" and r == "search" for r, n, _ in reasons)      # has a price: no searching for it
    assert q["counts"]["no_price"] == 1 and q["site_gap"] == 30

    client = await hass_client()
    page = '<a href="/nl/nl/p/lego-icons-orchidee-10311/9300000099/">LEGO Icons 10311 Orchidee</a>'
    r = await client.post("/api/lego_tracker/relay", json={"heartbeat": {"on": True, "done": 3, "ok": 2, "version": "x"},
                          "results": [{"kind": "search", "retailer": "amazon_de", "set_number": "10311", "url": "https://www.amazon.de/s?k=LEGO+10311", "status": 200, "html": "nothing"},
                                      {"kind": "search", "retailer": "c_none", "set_number": "10311", "status": 200, "html": page}]})
    body = await r.json()
    assert body["fail"] == 1 and len(body["rejected"]) == 1 and c.store["relay_heartbeat"]["done"] == 3
    assert "10311|amazon_de" in c.store["relay_searched"]
    assert not any(i["kind"] == "search" and i["set_number"] == "10311" and i["retailer"] == "amazon_de"
                   for i in c.continuous_items(50)["items"] if i.get("kind"))             # not searched again this week
    # a price from the browser is marked, a server price clears the mark
    r = await client.post("/api/lego_tracker/relay", json={"results": [{"set_number": "10281", "retailer": "bol",
                          "url": offers["10281"]["bol"]["url"], "price": 41.5, "title": "LEGO Bonsai 10281"}]})
    assert (await r.json())["ok"] == 1 and offers["10281"]["bol"]["last_via"] == "relay"
    # an open error is solved by a browser price: it leaves the error list (also when it was ignored)
    offers["10311"]["bol"]["ignored_error"] = "blocked (HTTP 403)"
    r = await client.post("/api/lego_tracker/relay", json={"results": [{"set_number": "10311", "retailer": "bol",
                          "url": offers["10311"]["bol"]["url"], "price": 39.5, "title": "LEGO Orchidee 10311"}]})
    assert (await r.json())["ok"] == 1 and offers["10311"]["bol"]["error"] is None and "ignored_error" not in offers["10311"]["bol"]
    assert not any(f["set_number"] == "10311" for f in c.retailer_stats()["bol"]["failing"])
    assert any("open error solved by your browser" == e["message"] for e in c.store["activity"] if e.get("set_number") == "10311")
    ws_card = __import__("custom_components.lego_tracker.websocket_api", fromlist=["_card"])._card(c, "10281")
    assert ws_card["offers"]["bol"]["via"] == "relay"
    await c.refresh_set("10281", ["bol"])
    assert "last_via" not in offers["10281"]["bol"]
    found = '<a href="/nl/nl/p/lego-technic-ferrari-daytona-sp3-42143/9300000088/">x</a>'
    r = await client.post("/api/lego_tracker/relay", json={"results": [{"kind": "search", "retailer": "bol", "set_number": "42143",
                          "url": "https://www.bol.com/nl/nl/s/?searchtext=42143", "status": 200, "html": found}]})
    body = await r.json()
    assert body["follow"][0]["url"] == "https://www.bol.com/nl/nl/p/lego-technic-ferrari-daytona-sp3-42143/9300000088/"
    assert offers["42143"]["bol"]["url"] == body["follow"][0]["url"]                # linked: the browser fetches it next


def test_sitemap_parsing_and_matching():
    import gzip as _gz

    from custom_components.lego_tracker import sitemaps

    assert sitemaps.robots_sitemaps("User-agent: *\nSitemap: https://www.smythstoys.com/be/sitemap.xml\n") == ["https://www.smythstoys.com/be/sitemap.xml"]
    index = '<sitemapindex><sitemap><loc>https://x.be/sitemap-cms.xml</loc></sitemap><sitemap><loc>https://x.be/sitemap-products-1.xml.gz</loc></sitemap></sitemapindex>'
    kids, pages = sitemaps.parse(index)
    assert pages == [] and sitemaps.order_children(kids)[0].endswith("products-1.xml.gz")
    urlset = ('<urlset><url><loc>https://www.smythstoys.com/be/nl-be/speelgoed/lego/lego-icons-10368-chrysant/p/236401</loc></url>'
              '<url><loc>https://www.smythstoys.com/be/nl-be/speelgoed/lego/led-verlichting-voor-lego-10368/p/999001</loc></url>'
              '<url><loc>https://www.smythstoys.com/be/nl-be/speelgoed/playmobil/71234-boot/p/111</loc></url>'
              '<url><loc>https://www.smythstoys.com/be/nl-be/speelgoed/lego/lego-city-60510-brandweer/p/10368</loc></url></urlset>')
    assert sitemaps.body_text(_gz.compress(urlset.encode())) == urlset                   # .xml.gz files
    urls = sitemaps.lego_urls(sitemaps.parse(urlset)[1], "smythstoys.com")
    assert len(urls) == 3                                                                 # no Playmobil
    assert sitemaps.match(urls, "10368").endswith("lego-icons-10368-chrysant/p/236401")   # not the LED kit, not product code 10368
    assert sitemaps.match(urls, "60510").endswith("/p/10368")
    assert sitemaps.match(urls, "42143") is None


async def test_links_from_sitemap_ean_and_redirect(hass: HomeAssistant, entry, no_network):
    from custom_components.lego_tracker.client import Fetcher

    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10368"}, blocking=True)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "60510"}, blocking=True)
    files = {
        "https://www.dreamland.be/robots.txt": b"Sitemap: https://www.dreamland.be/sitemap_index.xml",
        "https://www.dreamland.be/sitemap_index.xml": b"<sitemapindex><sitemap><loc>https://www.dreamland.be/sitemap_products.xml</loc></sitemap></sitemapindex>",
        "https://www.dreamland.be/sitemap_products.xml": b"<urlset><url><loc>https://www.dreamland.be/e/nl/dl/lego-icons-chrysant-10368-123456</loc></url></urlset>",
    }

    async def get_raw(rid, url):
        return (200, files[url], None) if url in files else (404, b"", "HTTP error 404")
    with patch.object(c.fetcher, "get_raw", get_raw):
        res = await c.refresh_sitemap("dreamland_be")
    assert res == {"urls": 1, "files": 2, "found": 1}          # the index + the product file (robots.txt aside)
    assert c.store["offers"]["10368"]["dreamland_be"]["url"] == "https://www.dreamland.be/e/nl/dl/lego-icons-chrysant-10368-123456"
    assert c.store["offers"]["10368"]["dreamland_be"]["found_via"] == "sitemap"
    # a rejected link is never taken from the sitemap again
    del c.store["offers"]["10368"]["dreamland_be"]
    c.store.setdefault("rejected", {})["10368"] = ["/e/nl/dl/lego-icons-chrysant-10368-123456"]
    assert c.sitemap_link("dreamland_be", "10368") is None

    # EAN fallback (in a job): the search for the number finds nothing, the EAN search does
    c.store["sets"]["60510"]["ean"] = "5702017583556"
    seen = []

    async def discover(retailer, num, force=False, url=None):
        seen.append(url)
        return "https://www.kruidvat.be/nl/lego-city-60510/p/123" if url and "5702017583556" in url else None
    c.job = {"running": True, "shops": {}}
    with patch.object(c.fetcher, "discover", discover):
        await c.discover_set("60510", ["kruidvat_be"])
    c.job = None
    assert seen[-1].endswith("text=5702017583556") and c.store["offers"]["60510"]["kruidvat_be"]["url"].endswith("/p/123")

    # a search that jumps straight to the product page (redirect) is taken
    f = Fetcher(None, use_impersonation=False)
    f.final_url["kruidvat_be"] = "https://www.kruidvat.be/nl/lego-city-brandweerkazerne-60510/p/777"
    page = "<html><title>LEGO City 60510 Brandweerkazerne | Kruidvat</title></html>"
    assert f._landed_on_product("kruidvat_be", "https://www.kruidvat.be/nl/search?text=60510", page, "60510").endswith("/p/777")
    assert f._landed_on_product("kruidvat_be", "https://www.kruidvat.be/nl/search?text=60510", page.replace("60510", "1"), "60510") is None


async def test_rendered_search_page_from_background_tab(hass: HomeAssistant, entry, no_network):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10368"}, blocking=True)
    # the server saw a JavaScript search page: the shop is marked, the continuous check asks for a background tab
    c.fetcher.discover_error["dreamland_be"] = "the search page does not contain 10368: this shop probably loads its results with JavaScript. Paste the product page URL instead."
    c._note_js("dreamland_be")
    item = next(i for i in c.continuous_items(50)["items"] if i.get("kind") == "search" and i["retailer"] == "dreamland_be")
    assert item["render"] is True
    html = '<div><a href="/e/nl/dl/lego-icons-chrysant-10368-123456"><span>LEGO Icons 10368 Chrysant</span></a></div>'
    res = c.relay_result({"kind": "search", "rendered": True, "retailer": "dreamland_be", "set_number": "10368",
                          "url": item["url"], "status": 200, "html": html})
    assert res["url"] == "https://www.dreamland.be/e/nl/dl/lego-icons-chrysant-10368-123456"
    assert c.store["offers"]["10368"]["dreamland_be"]["url"] == res["url"]


async def test_review_fixes_low_price_relay_search_and_via(hass: HomeAssistant, entry, no_network):
    from custom_components.lego_tracker.models import is_suspicious_price
    from custom_components.lego_tracker.websocket_api import _card

    new_set = {"set_number": "75192"}
    assert not is_suspicious_price(100.0, new_set, {}, [100.0, 1000.0])      # one other shop says the same price
    assert is_suspicious_price(19.99, new_set, {}, [749.99, 759.0])          # two shops that agree: an accessory

    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10368"}, blocking=True)
    # a blocked / failed browser search is not "searched" for a week
    c.relay_result({"kind": "search", "retailer": "amazon_de", "set_number": "10368", "url": "https://www.amazon.de/s?k=10368", "status": 403, "html": ""})
    c.relay_result({"kind": "search", "retailer": "amazon_nl", "set_number": "10368", "url": "https://www.amazon.nl/s?k=10368", "status": 0, "html": ""})
    assert not any(k.startswith("10368|amazon") for k in c.store.get("relay_searched", {}))
    # no ⓤ next to a manual price
    o = c.store["offers"]["10368"]["bol"] = {"url": "https://www.bol.com/nl/nl/p/x-10368/1/", "history": [[1, 30.0]], "available": True,
                                              "last_price": 30.0, "last_via": "relay"}
    assert _card(c, "10368")["offers"]["bol"]["via"] == "relay"
    o["manual_price"] = {"price": 25.0}
    assert _card(c, "10368")["offers"]["bol"]["via"] is None


def test_review_fixes_hosts_sizes_groups_and_lego_urls():
    import gzip as _gz
    import zlib as _zlib

    from custom_components.lego_tracker import sitemaps
    from custom_components.lego_tracker.client import check_url, registrable
    from custom_components.lego_tracker.models import agreeing_group, is_suspicious_price
    from custom_components.lego_tracker.parsers import normalize_url

    # a host that merely contains the shop's domain is not the shop
    evil = ["https://smythstoys.com.evil.example/lego-icons-10368/p/1", "https://www.smythstoys.com/be/lego-icons-10368/p/2"]
    assert sitemaps.lego_urls(evil, "smythstoys.com") == evil[1:]
    files, urls = sitemaps.read_file(b"<sitemapindex><sitemap><loc>http://127.0.0.1/admin.xml</loc></sitemap>"
                                     b"<sitemap><loc>https://www.smythstoys.com/p1.xml</loc></sitemap></sitemapindex>", "smythstoys.com", 10)
    assert files == ["https://www.smythstoys.com/p1.xml"]                       # never a sitemap on another host
    with pytest.raises(ValueError):
        normalize_url("bol", "https://bol.com.evil.example/nl/p/x/1/")
    assert normalize_url("bol", "https://www.bol.com/nl/nl/p/x/1/").startswith("https://www.bol.com/")
    # redirects: only within the same site
    assert registrable("www.amazon.com.be") == "amazon.com.be" and registrable("ocean.kieskeurig.be") == "kieskeurig.be"
    check_url("https://www.smythstoys.com/be/x", "smythstoys.com")
    for bad in ("http://169.254.169.254/latest", "http://localhost:8123/api", "file:///etc/passwd", "https://smythstoys.com.evil.example/"):
        with pytest.raises(ValueError):
            check_url(bad, "smythstoys.com")
    # a .gz file that unpacks to more than allowed is skipped, a normal one is read
    assert sitemaps.body_text(_gz.compress(b"<urlset></urlset>")) == "<urlset></urlset>"
    bomb = _zlib.compressobj(9, _zlib.DEFLATED, 16 + _zlib.MAX_WBITS)
    big = bomb.compress(b"0" * (sitemaps.MAX_TEXT + 10)) + bomb.flush()
    assert sitemaps.body_text(big) == ""
    # agreement in groups: two shops at 100 and two at 400 still make 5 an outlier
    assert agreeing_group([100, 100, 400, 400]) == [100, 100]
    assert is_suspicious_price(5.0, {"set_number": "1"}, {}, [100, 100, 400, 400])
    assert not is_suspicious_price(100.0, {"set_number": "1"}, {}, [100, 1000])



async def test_lost_decimal_comma_is_repaired_and_absurd_rrp_ignored(hass: HomeAssistant, entry, no_network):
    """Verify comma repairs require price evidence and preserve plausible high amounts."""
    from custom_components.lego_tracker.models import is_suspicious_price

    # the RRP typed on a phone came in as 16499 (164,99): shops that agree are not rejected because of it
    s = {"set_number": "10327", "rrp": 16499.0}
    assert not is_suspicious_price(149.99, s, {}, [159.99, 164.99])
    assert is_suspicious_price(149.99, s, {}, [])                             # nothing to compare with: the RRP still counts
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10327"}, blocking=True)
    c.store["sets"]["10327"]["rrp"] = 16499.0
    c.update_set("10327", {"owned": True, "paid": 129.99})
    c.store["collection"]["10327"]["paid"] = 12999.0
    c._fix_lost_commas()                                                       # no evidence (no shop prices): left alone
    assert c.store["sets"]["10327"]["rrp"] == 16499.0 and c.store["collection"]["10327"]["paid"] == 12999.0
    c.store["offers"]["10327"] = {"bol": {"url": "https://www.bol.com/nl/nl/p/x/1/", "available": True, "last_price": 159.99, "history": []}}
    c._fix_lost_commas()                                                       # the shops ask ~160: 16499 lost its comma
    assert c.store["sets"]["10327"]["rrp"] == 164.99 and c.store["collection"]["10327"]["paid"] == 129.99
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "75192"}, blocking=True)
    c.store["sets"]["75192"]["rrp"] = 2500.0                                   # a real, high amount stays as it is
    c.store["offers"]["75192"] = {"bol": {"url": "https://www.bol.com/nl/nl/p/y/2/", "available": True, "last_price": 2399.0, "history": []}}
    c._fix_lost_commas()
    assert c.store["sets"]["75192"]["rrp"] == 2500.0


async def test_suspicious_price_can_be_approved(hass: HomeAssistant, entry, no_network, hass_ws_client):
    """Verify approval records a held price, accepts nearby prices, and cannot be repeated."""
    from custom_components.lego_tracker.models import query_activity

    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10327"}, blocking=True)
    c.store["sets"]["10327"]["rrp"] = 164.99
    url = "https://www.bol.com/nl/nl/p/lego-icons-dune-10327/9300000157956163/"
    c.store["offers"]["10327"]["bol"] = {"url": url, "history": []}
    with pytest.raises(ValueError):
        c.report_price(19.99, url=url, set_number="10327", retailer="bol")           # under 20 % of RRP: held back
    o = c.store["offers"]["10327"]["bol"]
    assert o["suspect"]["price"] == 19.99 and not o.get("history")
    assert query_activity(c.store, status="suspect")["entries"]
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/offer/approve", "set_number": "10327", "retailer": "bol"})
    r = await ws.receive_json()
    assert r["success"] and r["result"]["price"] == 19.99
    assert o["approved"] == 19.99 and "suspect" not in o and o["history"][-1][1] == 19.99
    c.report_price(20.49, url=url, set_number="10327", retailer="bol")                 # prices like it count from now on
    assert o["last_price"] == 20.49
    await ws.send_json({"id": 2, "type": "lego_tracker/offer/approve", "set_number": "10327", "retailer": "bol"})
    assert not (await ws.receive_json())["success"]                                   # nothing left to approve


async def test_developer_tools(hass: HomeAssistant, entry, no_network, hass_ws_client):
    """Verify developer tools report state, preview and remove outliers, reset caches, and parse pages."""
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281"}, blocking=True)
    c.store["offers"]["10281"]["bol"] = {"url": "https://www.bol.com/nl/nl/p/x-10281/9300000012345678/",
                                         "history": [[1, 39.99], [2, 41.0], [3, 4100.0], [4, 40.5], [5, 39.0]]}
    c.store["relay_searched"] = {"10281|bol": 1}
    ws = await hass_ws_client(hass)

    async def call(i, **kw):
        """Send a developer-tool websocket command and return its response."""
        await ws.send_json({"id": i, "type": "lego_tracker/dev/tool", **kw})
        return await ws.receive_json()

    r = await call(1, action="stats")
    assert r["success"] and r["result"]["sets"] == 1 and r["result"]["history_points"] == 5
    r = await call(2, action="outliers")
    assert [p["price"] for p in r["result"]["points"]] == [4100.0] and len(c.store["offers"]["10281"]["bol"]["history"]) == 5
    r = await call(3, action="outliers", apply=True)
    assert len(c.store["offers"]["10281"]["bol"]["history"]) == 4
    r = await call(4, action="reset", what="relay_searched")
    assert r["result"]["removed"] == 1 and c.store["relay_searched"] == {}
    assert not (await call(5, action="reset", what="everything"))["success"]
    page = '<script type="application/ld+json">{"@type":"Product","name":"LEGO 10281","offers":{"price":"39.99","priceCurrency":"EUR"}}</script>'
    r = await call(6, action="parse", html=page, url="https://www.bol.com/nl/nl/p/x/1/")
    assert r["result"]["price"] == 39.99 and r["result"]["retailer"] == "bol"
    r = await call(7, action="dump")
    assert r["success"] and "stats" in r["result"]


async def test_watch_since_is_kept_for_sorting(hass: HomeAssistant, entry, no_network):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281"}, blocking=True)
    since = c.store["sets"]["10281"]["watch_since"]
    assert since <= time.time()
    c.update_set("10281", {"watch": False})
    assert "watch_since" not in c.store["sets"]["10281"]
    c.update_set("10281", {"watch": True})
    assert c.store["sets"]["10281"]["watch_since"] >= since


def test_news_file_parsing():
    from custom_components.lego_tracker import news

    text = open("news/nieuws.txt", encoding="utf-8").read()
    items = news.parse(text)
    assert len(items) >= 3 and all(i["title"] and i["body"] for i in items)
    nl = news.for_language(items, "nl")
    assert any("BETA" in i["title"] for i in nl) and all(i.get("lang") in ("", None, "nl") for i in nl)
    assert any(i["link"] == "/hacs/dashboard" for i in nl)
    en = news.for_language(items, "de")                       # no German news: English
    assert en and all(i.get("lang") == "en" for i in en)
    bad = news.parse("title: x\nlink: javascript:alert(1)\n\nbody\n---\ntitle: y\nlink: //evil.example/x\n\nb")
    assert [i["link"] for i in bad] == ["", ""]


async def test_ticker_market_tick_and_shop_link(hass: HomeAssistant, entry, no_network):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281"}, blocking=True)
    c.log("ok", "price", "€49.99 → €39.99", set_number="10281", retailer="bol", url="https://www.bol.com/nl/nl/p/x/1/",
          price=39.99, old_price=49.99, source="server")
    c.store["events"] = [{"ts": time.time(), "kind": "deal", "set_number": "10281", "name": "Bonsai", "price": 39.99,
                          "retailer": "bol", "url": "https://www.bol.com/nl/nl/p/x/1/", "discount": 20, "score": 80}]
    from custom_components.lego_tracker.news import NewsFeed
    c.news = NewsFeed(lambda: None)
    c.news.ts, c.news.items = time.time(), [{"id": "a", "title": "Hi", "body": "x", "link": "", "lang": ""}]
    tk = await c.ticker_data("nl")
    assert [i["kind"] for i in tk["items"]] == ["price", "deal"] and tk["items"][0]["pct"] == -20.0 and tk["news"][0]["id"] == "a"
    c.hass.config_entries.async_update_entry(c.entry, options={**c.entry.options, "ticker": {"watch": False, "deals": True, "news": False, "max_deals": 1}})
    tk = await c.ticker_data("nl")
    assert [i["kind"] for i in tk["items"]] == ["deal"] and tk["news"] == []

    # market value: one set per tick, never more often than the spread allows
    calls = []

    async def fake_one(src, num, *a):
        calls.append((src, num))
        c._cstore(src)[num] = {"status": "ok", "ts": time.time(), "shops": [], "data": {"market_new": 80.0}}
    c._compare_one = fake_one
    c.market_tick()
    await hass.async_block_till_done()
    assert calls == [("brickeconomy", "10281")]
    c.market_tick()
    await hass.async_block_till_done()
    assert len(calls) == 1                                       # next one waits (spread over the day)

    # notifications link to the product page of the cheapest shop, not a comparison page
    c.store["offers"]["10281"] = {
        "bol": {"url": "https://www.bol.com/nl/nl/p/x/1/", "available": True, "last_price": 39.99, "history": []},
        "amazon_nl": {"url": "https://www.kieskeurig.be/lego/product/123", "available": True, "last_price": 35.0, "history": []}}
    assert c.notifier.shop_link("10281", {"best_url": "https://www.kieskeurig.be/lego/product/123"}) == "https://www.bol.com/nl/nl/p/x/1/"


async def test_market_value_via_userscript_when_server_fails(hass: HomeAssistant, entry, no_network, hass_client):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281"}, blocking=True)
    assert not any(i.get("reason") == "market" for i in c.continuous_items(100)["items"])      # never failed: server does it
    c._cstore("brickeconomy")["10281"] = {"status": "error", "ts": time.time(), "error": "blocked (HTTP 403)"}
    q = c.continuous_items(100)
    item = next(i for i in q["items"] if i.get("reason") == "market")
    assert item["kind"] == "page" and item["source"] == "brickeconomy" and q["items"][-1] is item    # last in the queue
    client = await hass_client()
    r = await client.post("/api/lego_tracker/relay", json={"results": [{**item, "status": 0, "html": "", "error": "timeout"}]})
    assert r.status == 200
    assert not any(i.get("reason") == "market" for i in c.continuous_items(100)["items"])      # tried: not again today


async def test_deal_filter_themes_prices_and_notifications(hass: HomeAssistant, entry, no_network):
    c = await _setup(hass, entry)
    for n in ("75192", "10281"):
        await hass.services.async_call(DOMAIN, "add_set", {"set_number": n}, blocking=True)
    c.store["sets"]["75192"].update(theme="Star Wars", pieces=7541)
    c.store["sets"]["10281"].update(theme="Botanicals", pieces=878)
    with pytest.raises(Exception):
        c.settings_validate({"deal_filter": {"min_price": 100, "max_price": 50}})
    opts = c.settings_validate({"deal_filter": {"themes_off": ["star-wars"], "max_price": "450,50"}})
    assert opts["deal_filter"]["max_price"] == 450.5
    hass.config_entries.async_update_entry(c.entry, options={**c.entry.options, "deal_filter": opts["deal_filter"]})
    assert c.deal_blocked("75192", {}) == "theme" and c.deal_blocked("10281", {"best_price": 40}) is None
    assert c.deal_blocked("10281", {"best_price": 500}) == "max_price"

    sent = []

    async def fake_send(rule, title, message, **kw):
        sent.append(title)
    c.notifier.send = fake_send
    c.notifier.rules[:] = [{"id": "r1", "enabled": True, "scope": {"type": "all"}, "triggers": ["any_change", "digest"],
                            "params": {}, "shops": [], "cooldown_hours": 0},
                           {"id": "r2", "enabled": True, "scope": {"type": "sets", "sets": ["75192"]}, "triggers": ["any_change"],
                            "params": {}, "shops": [], "cooldown_hours": 0}]
    before, after = {"best_price": 600.0}, {"best_price": 550.0, "best_retailer": "bol", "best_url": None}
    await c.notifier.on_set_change("75192", before, after)
    assert len(sent) == 1                                   # only the rule that picked this set by hand
    await c.notifier.on_set_change("10281", {"best_price": 45.0}, {"best_price": 40.0, "best_retailer": "bol", "best_url": None})
    assert len(sent) == 2
    sent.clear()
    await c.notifier.on_digest({"deals": [{"set_number": "75192", "name": "", "price": 550, "retailer": "bol", "discount": 30},
                                          {"set_number": "10281", "name": "", "price": 40, "retailer": "bol", "discount": 20}]})
    assert len(sent) == 1


async def test_brickeconomy_skipped_when_market_value_off(hass: HomeAssistant, entry, no_network):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281"}, blocking=True)
    hass.config_entries.async_update_entry(c.entry, options={**c.entry.options, "market_value": False})
    asked = []

    async def fake_one(src, num, *a, **kw):
        asked.append(src)
    c._compare_one = fake_one
    await c.compare_refresh("10281", sources=["brickeconomy", "kieskeurig"])
    assert asked == ["kieskeurig"]


@pytest.mark.parametrize("method", ["market_tick", "sitemap_tick"])
async def test_scheduled_ticks_run_on_event_loop(hass: HomeAssistant, entry, method):
    import asyncio
    from homeassistant.core import HassJob, is_callback

    c = await _setup(hass, entry)
    await c.add_set("10281")
    tick = getattr(c, method)
    assert is_callback(tick)
    loops = []

    async def run(*args):
        loops.append(asyncio.get_running_loop())

    with patch.object(c, "_compare_one", run), patch.object(c, "_sitemap_round", run), \
         patch.object(c, "sitemap_shops", return_value=["bol"]):
        hass.async_run_hass_job(HassJob(tick))
        await hass.async_block_till_done(wait_background_tasks=True)
    assert loops == [asyncio.get_running_loop()]


async def test_debug_toggle_overrides_debug_ancestor(hass: HomeAssistant, entry):
    import logging
    from custom_components.lego_tracker import devtools

    c = await _setup(hass, entry)
    logger = logging.getLogger("custom_components.lego_tracker")
    parent = logging.getLogger("custom_components")
    old_level, old_parent_level = logger.level, parent.level
    try:
        parent.setLevel(logging.DEBUG)
        assert devtools.set_debug(True)
        assert devtools.stats(c)["debug"]
        assert not devtools.set_debug(False)
        assert not devtools.stats(c)["debug"]
    finally:
        logger.setLevel(old_level)
        parent.setLevel(old_parent_level)


@pytest.mark.parametrize("best_url, fallback, expected_price, expected_retailer", [
    ("https://www.amazon.nl/dp/B012345678", True, 35.0, "amazon_nl"),
    ("https://www.kieskeurig.be/lego/product/123", True, 39.99, "bol"),
    ("https://www.amazon.nl/s?k=lego+10281", True, 39.99, "bol"),
    (None, True, 39.99, "bol"),
    ("https://www.kieskeurig.be/lego/product/123", False, 35.0, "amazon_nl"),
    (None, False, 35.0, "amazon_nl"),
])
async def test_notification_offer_values_stay_together(
    hass: HomeAssistant, entry, best_url, fallback, expected_price, expected_retailer,
):
    from custom_components.lego_tracker import digest
    from custom_components.lego_tracker.const import RETAILERS

    c = await _setup(hass, entry)
    await c.add_set("10281", name="Bonsai", rrp=100)
    product_url = "https://www.bol.com/nl/nl/p/x/1/"
    c.store["offers"]["10281"] = {
        "amazon_nl": {"url": best_url, "available": True, "last_price": 35.0},
        "bol": {"url": product_url, "available": fallback, "last_price": 39.99},
        "amazon_de": {"url": "https://www.amazon.de/dp/B012345678", "available": False, "last_price": 10},
    }
    c.async_set_updated_data(c.compute())
    after = c.data["statuses"]["10281"]
    expected_url = product_url if expected_retailer == "bol" else best_url
    expected_text = f"€{expected_price:.2f} at {RETAILERS[expected_retailer][0]}"
    c.store["notify_rules"] = validate_rules([{
        "id": "test", "name": "test", "triggers": ["back_in_stock", "digest"],
        "targets": [{"type": "event"}], "cooldown_hours": 0,
    }])
    with patch.object(c.notifier, "send", new_callable=AsyncMock) as send:
        await c.notifier.on_set_change("10281", {}, after)
        assert expected_text in send.call_args.args[2]
        assert send.call_args.kwargs["url"] == expected_url
        assert send.call_args.kwargs["data"]["price"] == expected_price

        d = digest(c)
        row = d["deals"][0]
        assert (row["price"], row["retailer"], row["url"]) == (expected_price, expected_retailer, expected_url)
        # Rendering the digest must preserve its selected offer even if the store changes.
        c.store["offers"]["10281"]["bol"]["last_price"] = 20
        await c.notifier.on_digest(d)
        assert expected_text in send.call_args.args[2]
        if expected_url:
            assert expected_url in send.call_args.args[2]
        else:
            assert product_url not in send.call_args.args[2]


@pytest.mark.parametrize("shops, expected_price, expected_retailer", [
    ([], 39.99, "bol"),
    (["amazon_nl"], 35.0, "amazon_nl"),
    (["amazon_nl", "amazon_de"], 45.0, "amazon_de"),
    (["amazon_nl", "amazon_de", "bol"], 39.99, "bol"),
])
async def test_notification_fallback_respects_rule_shops(
    hass: HomeAssistant, entry, shops, expected_price, expected_retailer,
):
    from custom_components.lego_tracker.const import RETAILERS

    c = await _setup(hass, entry)
    await c.add_set("10281", name="Bonsai", rrp=100)
    c.store["offers"]["10281"] = {
        "amazon_nl": {"url": "https://www.amazon.nl/s?k=lego+10281", "available": True, "last_price": 35.0},
        "bol": {"url": "https://www.bol.com/nl/nl/p/x/1/", "available": True, "last_price": 39.99},
        "amazon_de": {"url": "https://www.amazon.de/dp/B012345678", "available": True, "last_price": 45.0},
    }
    c.async_set_updated_data(c.compute())
    c.store["notify_rules"] = [_rule(triggers=["back_in_stock"], shops=shops)]
    with patch.object(c.notifier, "send", new_callable=AsyncMock) as send:
        await c.notifier.on_set_change("10281", {}, c.data["statuses"]["10281"])
        assert send.call_count == 1
        assert f"€{expected_price:.2f} at {RETAILERS[expected_retailer][0]}" in send.call_args.args[2]
        assert send.call_args.kwargs["url"] == c.store["offers"]["10281"][expected_retailer]["url"]
        assert send.call_args.kwargs["data"]["price"] == expected_price


def test_debug_dump_redacts_url_credentials():
    from custom_components.lego_tracker.devtools import redact

    out = redact({"a": ["https://user:pw@shop.be/p/1?id=5&token=abc", "see https://x.be/?apikey=1 now", 3]})
    assert out == {"a": ["https://***@shop.be/p/1?id=5&token=***", "see https://x.be/?apikey=*** now", 3]}


async def test_ticker_defaults_api_level_and_market_label(hass: HomeAssistant, entry, no_network, hass_ws_client):
    from custom_components.lego_tracker.const import API_LEVEL

    c = await _setup(hass, entry)
    assert c.ticker == {"watch": True, "deals": True, "news": True, "max_watch": 3, "max_deals": 3, "max_news": 3}
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281"}, blocking=True)
    c.store["offers"]["10281"]["bol"] = {"url": "https://www.bol.com/nl/nl/p/x/1/", "available": True, "last_price": 39.99,
                                         "last_checked": time.time(), "history": [[time.time(), 39.99]]}
    c.push_update()
    from custom_components.lego_tracker.news import NewsFeed
    c.news = NewsFeed(lambda: None)
    c.news.ts = time.time()
    tk = await c.ticker_data("nl")                  # no recent price change: the current price is shown anyway
    assert tk["items"][0]["set_number"] == "10281" and tk["items"][0]["price"] == 39.99
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/overview"})
    assert (await ws.receive_json())["result"]["api"] == API_LEVEL
    c.store["sets"]["10281"].update(exit_date_source="BrickEconomy", market={"source": "BrickEconomy"})
    c.store["activity"].append({"ts": 1, "level": "info", "kind": "fetch", "message": "not on BrickEconomy", "source": "BrickEconomy"})
    c._rename_market_source()
    assert c.store["sets"]["10281"]["exit_date_source"] == "Market value" and c.store["sets"]["10281"]["market"]["source"] == "Market value"
    assert c.store["activity"][-1]["source"] == "Market value" and "BrickEconomy" not in c.store["activity"][-1]["message"]


@pytest.mark.parametrize("price, retailer, expected_score", [
    (39.99, "bol", True),
    (49.99, "bol", False),
    (39.99, "amazon_nl", False),
    (49.99, "amazon_nl", False),
])
async def test_ticker_recent_price_score_matches_best_offer(hass: HomeAssistant, entry, price, retailer, expected_score):
    hass.config_entries.async_update_entry(entry, options={**entry.options, "ticker": {"news": False, "deals": False}})
    c = await _setup(hass, entry)
    await c.add_set("10281", name="Bonsai", rrp=100)
    c.store["offers"]["10281"] = {"bol": {"available": True, "last_price": 39.99}}
    c.store["activity"] = [{"kind": "price", "ts": time.time(), "set_number": "10281",
                            "price": price, "old_price": 59.99, "retailer": retailer}]
    c.push_update()
    score = c.data["statuses"]["10281"]["deal_score"]
    assert score > 0
    item = (await c.ticker_data("en"))["items"][0]
    assert item["price"] == price
    assert item["score"] == (score if expected_score else None)


@pytest.mark.parametrize("merge", [False, True])
async def test_backup_from_before_0919_gets_the_new_market_label(hass: HomeAssistant, entry, no_network, merge):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281"}, blocking=True)
    data = c.export_backup()
    data["sets"]["10281"]["exit_date_source"] = "BrickEconomy"
    if merge:
        del c.store["sets"]["10281"]
    c.import_backup(data, merge)
    assert c.store["sets"]["10281"]["exit_date_source"] == "Market value"


def _gz(text: str) -> bytes:
    """Encode fixture text as UTF-8 and compress it as a gzip download."""
    import gzip as _g
    return _g.compress(text.encode())


SETS_CSV = ("set_num,name,year,theme_id,num_parts,img_url\n"
            "10281-1,Bonsai Tree,2021,2,878,https://cdn.rebrickable.com/media/sets/10281-1.jpg\n"
            "10281-2,Bonsai Tree (second edition),2022,2,878,\n"
            "75192-1,Millennium Falcon,2017,3,7541,https://cdn.rebrickable.com/media/sets/75192-1.jpg\n"
            "5007-1,Book,2015,4,0,\n"
            "fig-0001-1,Minifig,2020,3,4,\n")
THEMES_CSV = "id,name,parent_id\n1,Icons,\n2,Botanical Collection,1\n3,Star Wars,\n4,Books,\n"


def test_setdb_parse_search_and_new():
    """Check set filtering, theme ancestry, search, and detection of added sets."""
    from custom_components.lego_tracker import setdb

    db = setdb.parse(_gz(SETS_CSV), _gz(THEMES_CSV))
    assert set(db) == {"10281", "75192"}                                   # first version, numbered, with pieces
    assert db["10281"][:5] == ["Bonsai Tree", 2021, "Icons", "Botanical Collection", 878]
    assert setdb.search(db, "falcon") == ["75192"] and setdb.search(db, "102") == ["10281"]
    assert setdb.find_new(db, {**db, "10368": ["Chrysanthemum", 2024, "Icons", "", 278, ""]}, first=False) == ["10368"]


async def test_setdb_refresh_new_sets_and_add(hass: HomeAssistant, entry, no_network, hass_ws_client):
    """Verify refresh, theme filtering, search, enrichment, save failure, and reload."""
    from datetime import date as _date

    c = await _setup(hass, entry)
    year = _date.today().year
    first = SETS_CSV + "".join(f"{60000 + i}-1,City set {i},{year},5,{100 + i},\n" for i in range(1000))
    themes = THEMES_CSV + "5,City,\n"
    files = {"sets": _gz(first), "themes": _gz(themes)}
    c._download = AsyncMock(side_effect=lambda url: files["sets" if "sets.csv" in url else "themes"])
    sent = []
    c.notifier.on_new_sets = AsyncMock(side_effect=lambda nums: sent.append(nums))
    original_save = c._setdb_store.async_save

    async def save_while_busy(data):
        assert c.setdb_info["busy"] and not c.setdb
        c.setdb_tick()
        await original_save(data)
        assert c.setdb_info["busy"] and not c.setdb

    with patch.object(c._setdb_store, "async_save", side_effect=save_while_busy), \
         patch.object(entry, "async_create_background_task") as background:
        fresh = await c.refresh_setdb()
        background.assert_not_called()
    assert not c.setdb_info["busy"]
    assert len(fresh) == 1000 and not sent                                  # first download: this year's sets, no notification
    files["sets"] = _gz(first + f"76300-1,New Batman set,{year},6,500,\n42200-1,Technic car,{year},7,900,\n")
    files["themes"] = _gz(themes + "6,Batman,\n7,Technic,\n")
    hass.config_entries.async_update_entry(entry, options={**entry.options, "deal_filter": {"themes_off": ["Technic"]}})
    await hass.async_block_till_done()
    c = hass.data[DOMAIN][entry.entry_id]
    c._download = AsyncMock(side_effect=lambda url: files["sets" if "sets.csv" in url else "themes"])

    async def notify_while_busy(nums):
        assert c.setdb_info["busy"] and "76300" in c.setdb
        sent.append(nums)

    c.notifier.on_new_sets = AsyncMock(side_effect=notify_while_busy)
    fresh = await c.refresh_setdb()
    assert not c.setdb_info["busy"]
    assert sorted(fresh) == ["42200", "76300"] and sent == [["76300"]]     # Technic switched off: no notification
    items = c.new_sets()["items"]
    assert items[0]["set_number"] in ("76300", "42200") and "42200" not in [x["set_number"] for x in items]
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/setdb/search", "q": "batman"})
    r = (await ws.receive_json())["result"]
    assert r["items"][0]["set_number"] == "76300" and r["count"] == len(c.setdb)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "76300"}, blocking=True)
    s = c.store["sets"]["76300"]
    assert s["name"] == "New Batman set" and s["theme"] == "Batman" and s["pieces"] == 500 and s["name_source"] == "Rebrickable"
    # a failed save changes nothing in memory
    before, seen = dict(c.setdb), dict(c.store["new_sets"])
    files["sets"] = _gz(first + f"76300-1,New Batman set,{year},6,500,\n42200-1,Technic car,{year},7,900,\n10999-1,Later set,{year},1,50,\n")
    with patch.object(c._setdb_store, "async_save", AsyncMock(side_effect=OSError("disk full"))):
        assert await c.refresh_setdb() == []
    assert not c.setdb_info["busy"]
    assert c.setdb == before and c.store["new_sets"] == seen and "disk full" in c.setdb_info["error"]
    # Home Assistant's Store logs a write error and returns normally: the read-back notices it
    with patch.object(c._setdb_store, "async_save", AsyncMock(return_value=None)):
        assert await c.refresh_setdb() == []
    assert c.setdb == before and c.store["new_sets"] == seen and "could not be written" in c.setdb_info["error"]
    # stored in its own file and read back on start
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert "76300" in hass.data[DOMAIN][entry.entry_id].setdb


@pytest.mark.parametrize("stage, cancelled", [("download", False), ("download", True), ("save", True)])
async def test_setdb_refresh_clears_busy_on_failure(hass: HomeAssistant, entry, stage, cancelled):
    import asyncio
    from custom_components.lego_tracker import setdb

    c = await _setup(hass, entry)
    new = {str(60000 + i): ["City set", 2026, "City", "", 100, ""] for i in range(1000)}

    async def fail(*args):
        assert c.setdb_info["busy"]
        raise asyncio.CancelledError if cancelled else OSError("download failed")

    c._download = AsyncMock(side_effect=fail if stage == "download" else None, return_value=b"")
    with patch.object(setdb, "parse", return_value=new), patch.object(c._setdb_store, "async_save", side_effect=fail):
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await c.refresh_setdb()
        else:
            assert await c.refresh_setdb() == []
    assert not c.setdb_info["busy"] and not c.setdb


async def test_new_set_notification_rule(hass: HomeAssistant, entry, no_network):
    """Check that new-set notifications respect all-set and theme-specific scopes."""
    c = await _setup(hass, entry)
    c.setdb = {"76300": ["New Batman set", 2026, "Batman", "", 500, ""], "10400": ["Icons thing", 2026, "Icons", "", 900, ""]}
    sent = []

    async def fake_send(rule, title, message, **kw):
        """Capture notification rule IDs, titles, and messages for assertions."""
        sent.append((rule["id"], title, message))
    c.notifier.send = fake_send
    c.notifier.rules[:] = [{"id": "a", "enabled": True, "scope": {"type": "all"}, "triggers": ["new_set"], "params": {}, "shops": []},
                           {"id": "t", "enabled": True, "scope": {"type": "themes", "themes": ["Icons"]}, "triggers": ["new_set"],
                            "params": {}, "shops": []}]
    await c.notifier.on_new_sets(["76300", "10400"])
    assert [x[0] for x in sent] == ["a", "t"] and "2" in sent[0][1] and "10400" in sent[1][1]


def test_news_body_without_empty_line_and_other_line_ends():
    """Check inline news bodies, body aliases, a BOM, and alternate line endings."""
    from custom_components.lego_tracker import news

    a = news.parse("id: x\ndate: 2026-10-03\ntitle: Hallo\nlink: /hacs/dashboard\nDe tekst.\nTweede regel.")
    b = news.parse("﻿id: y\r\ntitle: T\r\ntekst: Eerste regel\r\nTweede\r\n")
    c = news.parse("# comment\rtitle: Z\r\rMac line ends")
    assert a[0]["body"] == "De tekst.\nTweede regel." and a[0]["link"] == "/hacs/dashboard"
    assert b[0]["body"] == "Eerste regel\nTweede" and c[0]["body"] == "Mac line ends"


@pytest.mark.first_check
async def test_new_set_gets_prices_and_market_value_right_away(hass: HomeAssistant, entry, no_network):
    """A set that was never checked is fetched right after adding (one set at a time), with its market value;
    adding it again or a set that already has prices does not start another check."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    c = hass.data[DOMAIN][entry.entry_id]
    order = []

    async def compare(num, refresh=False, force=False, retry_missing=False, sources=None):
        order.append(("compare", num, tuple(sources or ())))
        return {}

    async def discover(retailer, num, force=False, url=None):
        return f"https://www.amazon.nl/dp/B0{num}0" if retailer == "amazon_nl" else None

    with patch.object(c.fetcher, "discover", discover), patch.object(c, "compare_refresh", side_effect=compare):
        await c.add_set("10281")
        await c.add_set("42143")
        await hass.async_block_till_done(wait_background_tasks=True)
    linked = sum(len(c.store["offers"][n]) for n in ("10281", "42143"))
    assert linked == 2 and no_network.await_count == 2      # the linked shop of both sets, right away
    assert [x[1] for x in order] == ["10281", "42143"]       # one after the other, in the order they were added
    for num in ("10281", "42143"):
        assert c.store["offers"][num]["amazon_nl"].get("last_checked")
        assert any(e["kind"] == "check" and e.get("set_number") == num and e.get("source") == "added"
                   for e in c.store["activity"])
    assert not c._first_busy and not c._first_checks and c.job_info()["first_checks"] == []

    no_network.reset_mock()
    order.clear()
    await c.add_set("10281")                                  # already has prices: no extra check
    await hass.async_block_till_done(wait_background_tasks=True)
    assert no_network.await_count == 0 and not order

    # comparison sites switched off: the market value is still fetched for a new set
    hass.config_entries.async_update_entry(entry, options={**entry.options, "compare_sites": False})
    await hass.async_block_till_done()
    c = hass.data[DOMAIN][entry.entry_id]
    with patch.object(c.fetcher, "discover", AsyncMock(return_value=None)), \
         patch.object(c, "compare_refresh", side_effect=compare):
        await c.add_set("21028")
        await hass.async_block_till_done(wait_background_tasks=True)
    assert order == [("compare", "21028", ("brickeconomy",))]

    # a set removed while waiting is skipped
    c._first_checks.extend(["99999"])
    c._first_busy = True
    await c._run_first_checks()
    assert not c._first_busy and not c._first_checks


def test_scan_helpers():
    """Which sets of the database are looked up, in which order, and what counts as a deal."""
    from custom_components.lego_tracker import scan

    db = {"76300": ["Batman", 2026, "Batman", "", 500, ""], "10281": ["Bonsai", 2021, "Icons", "", 878, ""],
          "42200": ["Technic car", 2025, "Technic", "", 900, ""], "30650": ["Polybag", 2026, "City", "", 40, ""],
          "71000": ["Old set", 2018, "City", "", 300, ""], "75192": ["Falcon", 2024, "Star Wars", "", 7541, ""]}
    sc = {"75192": {"retired": "2024", "rts": 1}}
    cands = scan.candidates(db, sc, {"42200": {}}, {"technic"}, 50, None, year=2026)
    assert sorted(cands) == ["76300"]                                      # tracked, retired, old, too small, Technic: out
    assert sorted(scan.candidates(db, sc, {}, set(), year=2026)) == ["30650", "42200", "76300"]
    now = 1_000_000.0
    sc = {"76300": {"ts": now - 100}, "42200": {"ts": now - 50_000, "miss": 3}}
    assert scan.pick(["76300", "42200", "30650"], sc, db, now) == "30650"   # never looked up first
    assert scan.pick(["76300", "42200"], sc, db, now) == "76300"            # no offers 3 times: only after 14 days
    assert scan.pick(["42200"], sc, db, now) is None
    assert scan.needs_retired_check(None) and not scan.needs_retired_check({"rts": now}, now + 86400)
    shops = [{"retailer": None, "price": 10.0, "url": "x"}, {"retailer": "bol", "price": 12.0, "url": "y"},
             {"retailer": "amazon_nl", "price": 70.0, "url": "a"}, {"retailer": "bol", "price": 75.0, "url": "b"}]
    assert scan.best_offer(shops, {"bol": 1, "amazon_nl": 1}, 100.0) == {"price": 70.0, "shop": "amazon_nl", "url": "a"}
    e = {"price": 70.0, "rrp": 100.0}
    flt = {"min_price": None, "max_price": None, "min_discount": None}
    assert scan.discount(e) == 30.0 and scan.deal(e, 25, flt) == 30.0 and scan.deal(e, 35, flt) is None
    assert scan.deal(e, 25, {**flt, "max_price": 50}) is None and scan.deal(e, 25, {**flt, "min_discount": 40}) is None
    assert scan.deal({"price": 10.0, "rrp": 100.0}, 25, flt) is None       # 90 % off: not the set
    assert scan.deal({**e, "retired": "2025"}, 25, flt) is None
    assert [scan.status(x) for x in (None, {"ts": 1}, {"ts": 1, "price": 9}, {"deal": 30}, {"retired": "2024"})] == \
        ["unknown", "none", "sale", "deal", "retired"]


async def test_scan_sets_deals_retirement_and_catalog(hass: HomeAssistant, entry, no_network, hass_ws_client):
    """Sets of the database are looked up on a comparison site, retired sets on the market value page are
    left out from then on, a deal is notified once (again only when cheaper), and Deals → All LEGO sets shows it all."""
    from custom_components.lego_tracker import compare

    c = await _setup(hass, entry)
    year = time.localtime().tm_year
    c.setdb = {"76300": ["New Batman set", year, "Batman", "", 500, ""], "10305": ["Lion Knights", year - 1, "Icons", "", 4514, ""],
               "60400": ["Police car", year, "City", "", 100, ""], "10281": ["Bonsai Tree", year - 5, "Icons", "", 878, ""]}
    pages = {"76300": (compare.Result("data", data={"retired": None, "retail": 100.0}),
                       compare.Result("offers", shops=[{"retailer": "bol", "price": 70.0, "url": "https://www.bol.com/p/1"}])),
             "10305": (compare.Result("data", data={"retired": "2025", "retail": 349.99}), None),
             "60400": (compare.Result("missing"), compare.Result("missing"))}
    asked = []

    async def page(src, num):
        asked.append((src, num))
        return pages[num][0 if src == "brickeconomy" else 1]
    notified = []
    c.notifier.on_catalog_deal = AsyncMock(side_effect=lambda num, e: notified.append((num, e["price"])))
    with patch.object(c, "_scan_page", side_effect=page):
        e = await c.scan_set("76300")
        assert e["price"] == 70.0 and e["rrp"] == 100.0 and e["deal"] == 30.0 and e["shop"] == "bol"
        r = await c.scan_set("10305")
        assert r["retired"] == "2025" and "price" not in r and ("kieskeurig", "10305") not in asked
        n = await c.scan_set("60400")
        assert n["miss"] == 1 and "price" not in n and "deal" not in n
        await hass.async_block_till_done()
        assert notified == [("76300", 70.0)]
        await c.scan_set("76300")                                          # same price: not notified again
        pages["76300"] = (pages["76300"][0], compare.Result("offers", shops=[{"retailer": "bol", "price": 65.0, "url": "u"}]))
        await c.scan_set("76300")                                          # 7 % cheaper: notified
        await hass.async_block_till_done()
        assert notified == [("76300", 70.0), ("76300", 65.0)]
        assert [x for x in asked if x == ("brickeconomy", "76300")] == [("brickeconomy", "76300")]   # retirement: once a month
        pages["76300"] = (pages["76300"][0], compare.Result("offers", shops=[{"retailer": "bol", "price": 95.0, "url": "u"}]))
        await c.scan_set("76300")
        assert "deal" not in c.scan["76300"] and "notified" not in c.scan["76300"]
        pages["76300"] = (pages["76300"][0], compare.Result("offers", shops=[{"retailer": "bol", "price": 70.0, "url": "u"}]))
        await c.scan_set("76300")
    assert sorted(c.scan_candidates()) == ["60400", "76300"]              # retired and old: out

    cat = c.catalog(status="deal")
    assert [x["set_number"] for x in cat["items"]] == ["76300"] and cat["items"][0]["discount"] == 30.0
    assert cat["items"][0]["shop"] == "bol.com" and cat["scan"]["counts"]["deal"] == 1 and cat["scan"]["counts"]["retired"] == 1
    assert [x["set_number"] for x in c.catalog(status="retired")["items"]] == ["10305"]
    assert [x["set_number"] for x in c.catalog(q="bonsai")["items"]] == ["10281"]
    assert c.catalog(sort="new")["items"][0]["year"] == year and c.catalog()["total"] == 4
    await c.add_set("76300", discover=False)                                # followed now: shown with its own prices
    assert c.catalog(q="batman")["items"][0]["status"] == "followed"
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/catalog", "status": "retired"})
    res = (await ws.receive_json())["result"]
    assert [x["set_number"] for x in res["items"]] == ["10305"] and res["count"] == 4

    # the tick looks up one due set in the background, then waits 86400 / sets-per-day seconds
    c.scan_info["next"] = 0
    with patch.object(c, "scan_set", AsyncMock()) as one:
        c.scan_tick()
        await hass.async_block_till_done(wait_background_tasks=True)
        one.assert_awaited_once_with("60400")
        assert not c.scan_info["busy"] and c.scan_info["next"] > time.time() + 200
        c.scan_tick()
        assert one.await_count == 1

    # comparison sites switched off: no price lookups; the setting is validated
    from custom_components.lego_tracker.i18n import LocalizedError
    for bad in (7, 100.7, "nan", "inf", "x", None):
        with pytest.raises(LocalizedError):
            c.settings_validate({"catalog_scan": bad})
    assert c.settings_validate({"catalog_scan": "600"})["catalog_scan"] == 600
    assert c.settings_validate({"catalog_scan": 0})["catalog_scan"] == 0
    hass.config_entries.async_update_entry(entry, options={**entry.options, "compare_sites": False, "catalog_scan": 600})
    await hass.async_block_till_done()
    c = hass.data[DOMAIN][entry.entry_id]
    assert c.scan_per_day == 600 and c._scan_source("76300") is None


async def test_scan_page_follows_and_handles_errors(hass: HomeAssistant, entry, no_network):
    """A comparison site's search page is followed to the product page; a refused page gives None (and the
    search page's prices when it had any), a missing page 'missing'."""
    from custom_components.lego_tracker import compare

    c = await _setup(hass, entry)
    answers = [(200, "<search>", None), (200, "<product>", None)]
    results = [compare.Result("follow", url="https://www.kieskeurig.be/lego/123", shops=[{"retailer": "bol", "price": 80.0}]),
               compare.Result("offers", shops=[{"retailer": "bol", "price": 79.0}])]
    with patch.object(c.fetcher, "get_page", AsyncMock(side_effect=lambda *a, **k: answers.pop(0))), \
         patch.object(compare, "parse", side_effect=lambda *a, **k: results.pop(0)), \
         patch.object(compare, "is_compare_url", return_value=True):
        res = await c._scan_page("kieskeurig", "76300")
        assert res.kind == "offers" and res.shops[0]["price"] == 79.0
        answers[:] = [(200, "<search>", None), (403, "", "blocked (HTTP 403)")]
        results[:] = [compare.Result("follow", url="https://www.kieskeurig.be/lego/123", shops=[{"retailer": "bol", "price": 80.0}])]
        res = await c._scan_page("kieskeurig", "76300")
        assert res.kind == "offers" and res.shops[0]["price"] == 80.0
        answers[:] = [(503, "", "busy")]
        assert await c._scan_page("kieskeurig", "76300") is None and "busy" in c.scan_info["error"]
        answers[:] = [(404, "", None)]
        assert (await c._scan_page("kieskeurig", "76300")).kind == "missing"
    assert c.scan_info["today"] == 6


async def test_catalog_deal_notification_rule(hass: HomeAssistant, entry, no_network):
    """A deal on a set you don't follow is sent to rules with that trigger, a theme rule only for its themes."""
    c = await _setup(hass, entry)
    c.setdb = {"76300": ["New Batman set", 2026, "Batman", "", 500, "https://img/1.jpg"]}
    sent = []

    async def fake_send(rule, title, message, **kw):
        sent.append((rule["id"], title, message, kw.get("url")))
    c.notifier.send = fake_send
    c.notifier.rules[:] = [{"id": "a", "enabled": True, "scope": {"type": "all"}, "triggers": ["catalog_deal"], "params": {}, "shops": []},
                           {"id": "t", "enabled": True, "scope": {"type": "themes", "themes": ["Icons"]}, "triggers": ["catalog_deal"],
                            "params": {}, "shops": []},
                           {"id": "n", "enabled": True, "scope": {"type": "all"}, "triggers": ["new_set"], "params": {}, "shops": []}]
    await c.notifier.on_catalog_deal("76300", {"price": 70.0, "shop": "bol", "url": "https://www.bol.com/p/1", "rrp": 100.0, "deal": 30.0})
    assert len(sent) == 1 and sent[0][0] == "a" and "76300" in sent[0][1] and "70.00" in sent[0][2] and "30%" in sent[0][2]
    assert sent[0][3] == "https://www.bol.com/p/1"
