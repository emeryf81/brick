# lot – Lego Organizing Tool

LEGO Price Tracker voor Home Assistant

Custom integration (HACS-compatibel) die LEGO-sets en hun prijzen volgt bij **Amazon.nl, Amazon.de, Amazon.com.be, bol.com en Kruidvat.be**, met een dagelijks dashboard, prijsgrafieken en collectiewaarde.

> Status: 0.3.0. Getest met unit- en integratietests (39) tegen een echte Home Assistant-core (2026.2.3): config flow, options flow, herladen, services, sensoren, websocket en CSV-import. Het paneel is in Chromium gerenderd met nagemaakte data. **Nog niet gedaan:** een run op jouw HA OS 2026.9 en een controle van de winkel-parsers tegen de live sites. Verwacht dat je die parsers moet bijstellen.

## Wat het doet
- **Sidebar-paneel "LEGO"** (toont bovenaan de draaiende versie en het gebruikte request-type) met tabbladen:
  - **Vandaag**: 🔻 laagste prijs ooit · 🎯 streefprijs bereikt · 🏷️ korting ≥ drempel (schuifregelaar) · 📉 sterk gedaald in 30 dagen. Een waarschuwingsbalk toont aanbiedingen zonder prijs en gepauzeerde winkels.
  - **Alle sets**: filter op thema **en subthema** (Botanicals, Technic, Creator, Icons, City, Friends, …), zoeken, sorteren op korting / prijs / **prijs per steen** / grootste daling, "in bezit / wishlist". Elke kaart toont trend (7/30 dagen), ct per steen en streefprijs.
  - **Wishlist**: sets die je nog niet hebt, met totaalprijs nu, totale adviesprijs en besparing.
  - **Collectie**: groeigrafiek (waarde vs. aankoopkost), waarde per thema, winst/verlies per set, CSV-export.
  - **Toevoegen / import**: set toevoegen (met streefprijs), **meerdere setnummers plakken**, winkels opnieuw zoeken, winkel-link koppelen, CSV-import, **back-up en herstel (JSON)**.
  - Klik op een set: prijsgrafiek per winkel met **hover-tooltip** en bereik (30 d / 90 d / 1 jaar / alles), adviesprijs-lijn, laagste prijs per winkel, links, handmatige prijs, bewerken (thema, streefprijs, notitie, bezit).
- **Streefprijs per set**: bereikt de beste prijs je streefprijs, dan volgt een event én (optioneel) een melding.
- **Meldingen**: kies in de opties een notify-service (bv. `notify.mobile_app_telefoon`) en krijg een push bij nieuwe laagste prijs, hoge korting, streefprijs en de dagelijkse samenvatting (met link naar de winkel).
- **Sensoren**: gevolgde sets, sets op record-laag, sets met hoge korting, sets onder streefprijs, wishlist-kost, aanbiedingen met fout (met pauze-info), collectiewaarde/-kost/-groei, plus **één prijssensor per set** (attributen: prijs per steen, trend 7/30 dagen, streefprijs, per winkel). Home Assistant maakt daar zelf historiek- en statistiekgrafieken van.
- **Events** voor automations: `lego_tracker_new_all_time_low`, `lego_tracker_high_discount`, `lego_tracker_target_price_reached`, `lego_tracker_daily_digest`.
- **Diagnostics-download** (zonder geheimen) voor foutzoeken.

## Services
`add_set`, `add_sets`, `remove_set`, `set_offer`, `report_price`, `discover_offers`, `refresh`, `import_collection`, `export_collection` (CSV terug als response), `export_data` / `import_data` (volledige JSON-back-up), `send_digest`. Zie *Ontwikkelaarstools → Acties*.

Voorbeeld-automation:
```yaml
trigger:
  - platform: event
    event_type: lego_tracker_target_price_reached
action:
  - service: notify.mobile_app_telefoon
    data:
      title: "{{ trigger.event.data.name }}"
      message: "€{{ trigger.event.data.price }} bij {{ trigger.event.data.retailer }}"
```

## Installatie
1. HACS → Custom repositories → deze repo, categorie *Integration* (of kopieer `custom_components/lego_tracker` naar je `config/`).
2. Herstart HA → Instellingen → Apparaten & diensten → *LEGO Price Tracker* toevoegen.
3. Opties: kortingsdrempel, winkels, ververs-interval (standaard 6 u), uur van de samenvatting, optioneel een gratis [Brickset API-sleutel](https://brickset.com/tools/webservices/requestkey) voor naam/thema/adviesprijs/afbeelding.

## Sets en winkels
- Voeg een set toe via het paneel of `lego_tracker.add_set`. De integratie zoekt per winkel een productpagina (best effort). Klopt die niet, koppel de juiste met `lego_tracker.set_offer` (volledige URL, of een ASIN voor Amazon).
- **Prijshistoriek** wordt bewaard per winkel (één punt per dag, alleen bij wijziging) in `.storage/lego_tracker.data`.
- **Laagste prijs ooit** telt pas na minstens N dagen historiek (standaard 3) en als de prijs ooit anders was, zodat een net toegevoegde set niet meteen "record" is.
- **Korting** = t.o.v. adviesprijs; zonder adviesprijs t.o.v. de mediaan van de laatste 90 dagen.

## Collectie importeren
CSV (`,`, `;` of tab) met kolommen zoals *Number/Set Number, Name, Theme, Subtheme, Qty, Paid, Value, Purchase Date, Pieces, Retail Price*. Werkt met exports van BrickEconomy, Brickset, Rebrickable of een eigen sheet; de kolomnamen worden herkend via aliassen (`csv_import.py`). Dubbele regels van dezelfde set tellen als extra exemplaren. Via het paneel of:

```yaml
service: lego_tracker.import_collection
data:
  file_path: /config/lego_collection.csv   # moet in allowlist_external_dirs staan
  replace: false
```

**Collectiegroei**: de waarde wordt herrekend uit de eigen prijshistoriek (goedkoopste nieuwprijs × aantal). Zolang een set geen prijshistoriek heeft, wordt de geïmporteerde waarde (bv. de BrickEconomy-waarde uit je CSV) of anders de adviesprijs gebruikt. BrickEconomy heeft geen publieke gratis API, daarom is er geen rechtstreekse koppeling; de CSV-waarde is de brug.

## Blokkades (captcha / HTTP 403) en wat ertegen helpt
Amazon en bol.com herkennen Python-scrapers vooral aan de **TLS-vingerafdruk**, niet aan de headers. Drie lagen, in deze volgorde:

1. **Chrome nabootsen (standaard aan, optie "Chrome-browser nabootsen")**: de integratie installeert bij de eerste start `curl_cffi` en doet requests met Chrome's vingerafdruk, met een startpagina-bezoek, cookies per winkel en echte browser-headers. Als de installatie mislukt, valt ze terug op aiohttp (waarschuwing in het HA-log). Het paneel-log toont welk transport actief is.
2. **Pauze na een blokkade**: wordt een winkel geblokkeerd, dan wordt die 1 u, 3 u, 6 u, 12 u, daarna 24 u niet meer bevraagd. Doorhameren maakt de bescherming strenger. De aanbieding toont "paused … after being blocked".
3. **Prijzen aanleveren van buitenaf** (werkt altijd):
   - **Userscript** `tools/lego-tracker.user.js` (Tampermonkey/Violentmonkey): draait in *jouw eigen browser* op de productpagina's die je bezoekt en stuurt de prijs naar HA. Menu "HA instellen" → HA-URL en een long-lived access token. Alleen al gevolgde producten worden bijgewerkt.
   - **Service `lego_tracker.report_price`** (`price` + `url`, of `price` + `set_number` + `retailer`): voor n8n, een automation of een ander script.
   - **Handmatig** in het detailvenster van een set (veld "Handmatig").

Betere bronnen dan scrapen, als het blijft haperen: bol.com Marketing Catalog API (gratis partneraccount) en Amazon PA-API (vereist een partneraccount met verkopen). Die zijn nog niet ingebouwd, want ik kon ze niet verifiëren; `parsers.py` en `client.py` zijn de plek om ze in te pluggen.

## Overige beperkingen
- Amazon en bol.com **staan scraping niet toe** in hun voorwaarden. Gebruik het voor persoonlijk gebruik met een redelijk interval (standaard 6 u, 4–7 s tussen requests).
- Winkel-markup verandert; parsers proberen JSON-LD → meta tags → winkel-specifieke markup. Zie `tests/test_logic.py`.
- Eén winkel wordt sequentieel bevraagd; bij honderden sets duurt een volledige ronde lang.

## Ontwikkelen
```
# logica-tests (Python 3.11+)
pip install pytest && python -m pytest tests/test_logic.py
# volledige suite met Home Assistant (Python 3.13)
pip install pytest-homeassistant-custom-component home-assistant-frontend && python -m pytest tests
```
