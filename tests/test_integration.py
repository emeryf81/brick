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
        "refresh_mode": "times", "refresh_times": "8:00, 20.15", "language": "nl", "spread_hours": 12})
    assert out["type"] == "create_entry"
    assert entry.options["refresh_mode"] == "times" and entry.options["language"] == "nl" and entry.options["spread_hours"] == 12
    await hass.async_block_till_done()          # reload must not raise on re-registering panel/services
    assert entry.state.value == "loaded"


async def test_config_flow(hass: HomeAssistant):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["type"] == "form"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {
        "discount_threshold": 30, "retailers": ["bol", "kruidvat_be"], "digest_time": "08:00:00", "min_history_days": 3,
        "refresh_mode": "times", "refresh_times": "nooit"})
    assert r["type"] == "form" and r["errors"] == {"refresh_times": "invalid_times"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {
        "discount_threshold": 30, "retailers": ["bol", "kruidvat_be"], "digest_time": "08:00:00", "min_history_days": 3,
        "refresh_mode": "spread", "refresh_times": "19:30, 7:30"})
    assert r["type"] == "create_entry" and r["options"]["discount_threshold"] == 30
    assert r["options"]["refresh_mode"] == "spread" and r["options"]["language"] == "en" and r["options"]["auto_refresh"]
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
               AsyncMock(side_effect=lambda r, n, force=False: "https://www.amazon.nl/dp/B0LEDLEDLE" if r == "amazon_nl" else None)):
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


BW_PAGE = '''<html><head><title>LEGO 10281 Bonsai - Brickwatch</title><meta property="og:image" content="https://img.brickwatch.net/10281.jpg"></head>
<body><h1>LEGO® Icons 10281 Bonsaiboompje</h1><p>Adviesprijs € 49,99</p><table>
<tr><td><img alt="bol.com"></td><td><s>€ 49,99</s> € 36,49</td><td><a href="/nl-BE/go/1">Naar winkel</a></td></tr>
<tr><td><img alt="Amazon.nl"></td><td>€ 37,10</td><td><a href="https://www.amazon.nl/dp/B0BONSAI01">Naar winkel</a></td></tr>
<tr><td><img alt="Top1Toys"></td><td>€ 41,00</td><td><a href="/nl-BE/go/9">Naar winkel</a></td></tr></table></body></html>'''


def _pages(**by_host):
    """get_page mock: (status, html) per host fragment, 404 for the rest."""
    async def get(src, url, force=False):
        for frag, res in by_host.items():
            if frag.replace("_", ".") in url or frag in url:
                return res(url) if callable(res) else res
        return 404, "", None
    return AsyncMock(side_effect=get)


async def test_brickwatch_hidden_source(hass: HomeAssistant, entry, no_network, hass_ws_client):
    c = await _setup(hass, entry)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/compare/fetch"})
    assert (await ws.receive_json())["error"]["code"] == "not_enabled"          # off by default
    hass.config_entries.async_update_entry(entry, options={**entry.options, "brickwatch": True})
    await hass.async_block_till_done()
    c = hass.data[DOMAIN][entry.entry_id]
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281"}, blocking=True)
    await hass.services.async_call(DOMAIN, "set_offer", {"set_number": "10281", "retailer": "bol",
                                                         "url": "https://www.bol.com/nl/nl/p/lego-bonsai/9300000038297067/"}, blocking=True)
    no_network.side_effect = lambda rid, url, force=False: (None, "blocked (HTTP 403)")    # every shop blocks us
    page = _pages(brickwatch=(200, BW_PAGE, None))
    with patch.object(c.fetcher, "get_page", page):
        await c.refresh_all()
    urls = [a.args[1] for a in page.await_args_list]
    assert urls[0] == "https://www.brickwatch.net/nl-BE/set/10281/" and any("kieskeurig.be/search?q=lego+10281" in u for u in urls)
    offers = c.store["offers"]["10281"]
    assert offers["bol"]["last_price"] == 36.49 and offers["bol"]["error"] is None          # bol.com via Brickwatch
    assert offers["amazon_nl"]["via"] == "brickwatch" and offers["amazon_nl"]["last_price"] == 37.10   # new link via Brickwatch
    s = c.store["sets"]["10281"]
    assert s["rrp"] == 49.99 and s["image"] == "https://img.brickwatch.net/10281.jpg" and "Bonsaiboompje" in s["name"]
    check = [e for e in c.store["activity"] if e["kind"] == "check"][-1]
    assert check["results"]["bol"]["via"] == "brickwatch" and check["results"]["bol"]["ok"]
    shops = {sh["name"]: sh for sh in c.store["compare"]["brickwatch"]["10281"]["shops"]}
    assert shops["Top1Toys"]["retailer"] is None and shops["Top1Toys"]["price"] == 41.0      # every shop is kept
    assert c.store["compare"]["kieskeurig"]["10281"]["status"] == "missing"
    # the set dialog gets all shops per site; pages are re-used for a few hours
    await ws.send_json({"id": 2, "type": "lego_tracker/set", "set_number": "10281"})
    card = (await ws.receive_json())["result"]
    assert len(card["compare"]["brickwatch"]["shops"]) == 3 and card["compare"]["kieskeurig"]["status"] == "missing"
    n = page.await_count
    with patch.object(c.fetcher, "get_page", page):
        await c.refresh_set("10281")
    assert page.await_count == n
    await ws.send_json({"id": 3, "type": "lego_tracker/overview"})
    ov = (await ws.receive_json())["result"]["brickwatch"]
    assert ov["sources"]["brickwatch"]["sets"] == 1 and ov["sources"]["kieskeurig"]["missing"] == 1


async def test_brickwatch_missing_page_not_retried_within_a_day(hass: HomeAssistant, entry, no_network):
    hass.config_entries.async_update_entry(entry, options={**entry.options, "brickwatch": True, "compare_sources": ["brickwatch"]})
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "385"}, blocking=True)
    page = AsyncMock(return_value=(404, "", None))
    bw = lambda: c.store["compare"]["brickwatch"]   # noqa: E731
    with patch.object(c.fetcher, "get_page", page):
        assert await c.compare_refresh("385") == {}
        assert await c.compare_refresh("385", force=True) == {}                        # not even forced
        assert page.await_count == 1 and bw()["385"]["status"] == "missing"
        bw()["385"]["ts"] -= 25 * 3600                                                 # a day later
        await c.compare_refresh("385")
        assert page.await_count == 2
    # a redirect to the home page counts as missing too
    home = AsyncMock(return_value=(200, "<html><title>Brickwatch België</title><h1>Welkom</h1></html>", None))
    bw().pop("385")
    with patch.object(c.fetcher, "get_page", home):
        assert await c.compare_refresh("385") == {}
    assert bw()["385"]["status"] == "missing"
    assert "385" in c._spread_candidates()                                             # sets without links are checked too


BW_LED = """<html><head><title>LED verlichting voor LEGO 43290 - Brickwatch</title></head><body>
<h1>BriksMax LED-verlichtingsset voor LEGO 43290</h1>
<table><tr><td>Shop X</td><td>€ 29,99</td><td><a href="https://shopx.example/led">Naar winkel</a></td></tr></table>
<p>Hoort bij: <a href="/nl-BE/set/43290-1/lego-disney-magic-castle">LEGO Disney 43290 Het magische kasteel</a>
<a href="/nl-BE/set/43290-2/led-kit">LED kit 43290</a></p></body></html>"""
BW_REAL = """<html><head><title>LEGO 43290 - Brickwatch</title></head><body><h1>LEGO Disney 43290 Het magische kasteel</h1>
<div class="offer"><span class="shop">bol.com</span><span class="price">€ 79,99</span><a href="/nl-BE/go/7">Bekijk</a></div>
<div class="offer"><span class="shop">Dreamland</span><span class="price">€ 84,99</span><a href="/nl-BE/go/8">Bekijk</a></div>
</body></html>"""
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


def test_compare_parsers_skip_led_and_follow():
    from custom_components.lego_tracker import compare
    from custom_components.lego_tracker.shops import all_domains

    d = all_domains()
    # Brickwatch: an LED-kit page for the number is not the set: follow the link to the real set page
    r = compare.parse("brickwatch", BW_LED, "43290", "https://www.brickwatch.net/nl-BE/set/43290/", d)
    assert r.kind == "follow" and r.url == "https://www.brickwatch.net/nl-BE/set/43290-1/lego-disney-magic-castle"
    r = compare.parse("brickwatch", BW_LED.replace("/nl-BE/set/43290-1/lego-disney-magic-castle", "/x"), "43290",
                      "https://www.brickwatch.net/nl-BE/set/43290/", d)
    assert r.kind == "follow" and r.url == "https://www.brickwatch.net/nl-BE/search/?q=43290"      # or search the site
    r = compare.parse("brickwatch", BW_REAL, "43290", "https://www.brickwatch.net/nl-BE/set/43290-1/x", d, step=1)
    assert r.kind == "offers" and [(x["retailer"], x["price"]) for x in r.shops] == [("bol", 79.99), ("dreamland_be", 84.99)]
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
    # Producthero needs the EAN
    assert compare.first_url("producthero", "60454", "nl-be") is None
    assert compare.first_url("producthero", "60454", "nl-be", "5702016914177") == \
        "https://shopping.producthero.com/nl/product/05702016914177?country=be"
    assert compare.first_url("channable", "10328", "nl-be") == "https://shopping.channable.com/?country=BE&search=lego+10328"


async def test_compare_network_errors_pause_one_hour_and_job_stops(hass: HomeAssistant, entry, no_network):
    hass.config_entries.async_update_entry(entry, options={**entry.options, "brickwatch": True, "compare_sources": ["brickwatch"]})
    c = await _setup(hass, entry)
    for n in ("10281", "10300", "10305", "10311", "10313", "10316", "10317"):
        await hass.services.async_call(DOMAIN, "add_set", {"set_number": n}, blocking=True)
    err = "network error: Failed to perform, curl: (35) BoringSSL SSL_connect: Connection closed abruptly"
    page = AsyncMock(return_value=(0, "", err))
    with patch.object(c.fetcher, "get_page", page), \
            patch("homeassistant.helpers.event.async_call_later") as later:
        c.start_brickwatch()
        await c._job_task
    assert page.await_count == 5                                   # 5 network errors in a row: stop asking
    assert 3500 < c.fetcher.cooldown_left("brickwatch") <= 3600
    assert c.last_job["cancelled"] and later.call_args.args[1] > 3600    # the rest follows after the pause
    assert any("5 network errors in a row" in e["message"] for e in c.store["activity"])
    with patch.object(c.fetcher, "get_page", page):
        await c.compare_refresh("10281")                           # paused: skipped
    assert page.await_count == 5


async def test_relay_fetches_comparison_pages(hass: HomeAssistant, entry, no_network, hass_client):
    hass.config_entries.async_update_entry(entry, options={**entry.options, "brickwatch": True, "compare_sources": ["kieskeurig"]})
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
