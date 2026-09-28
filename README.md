# LEGO Price Tracker voor Home Assistant

Custom integration (HACS-compatibel) die LEGO-sets en hun prijzen volgt bij **Amazon.nl, Amazon.de, Amazon.com.be, bol.com en Kruidvat.be**, met een dagelijks dashboard, prijsgrafieken en collectiewaarde.

> Status: 0.1.0, eerste versie. Getest met unit tests en integratietests tegen een echte Home Assistant-core (2026.2.3): config flow, options flow, herladen, services, sensoren, websocket en CSV-import. Het paneel is in Chromium gerenderd met nagemaakte data. **Nog niet gedaan:** een run op jouw HA OS 2026.9 en een controle van de winkel-parsers tegen de live sites. Verwacht dat je die parsers moet bijstellen.

## Wat het doet
- **Sidebar-paneel "LEGO"** met tabbladen:
  - **Vandaag**: sets op *laagste prijs ooit* en sets met *korting ≥ drempel* (schuifregelaar, standaard 25%).
  - **Alle sets**: filter op thema/subthema (Botanicals, Technic, Creator, Icons, City, Friends, …), zoeken, sorteren, "in bezit / wishlist".
  - **Collectie**: groeigrafiek (waarde vs. aankoopkost), waarde per thema, lijst met winst/verlies per set.
  - **Toevoegen / import**: set toevoegen, winkel-link koppelen, CSV-import.
  - Klik op een set: prijsgrafiek per winkel (met adviesprijs), laagste prijs per winkel, links, bewerken.
- **Sensoren**: aantal gevolgde sets, aantal sets op record-laag, aantal met hoge korting, collectiewaarde/-kost/-groei, plus **één prijssensor per set** (Home Assistant maakt daar zelf historiek- en statistiekgrafieken van, bruikbaar in Lovelace).
- **Dagelijkse samenvatting** op een instelbaar uur: event `lego_tracker_daily_digest` + persistent notification. Ook events `lego_tracker_new_all_time_low` en `lego_tracker_high_discount` voor je eigen automations (bv. push-melding).

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

## Beperkingen, eerlijk
- Amazon en bol.com **staan scraping niet toe** in hun voorwaarden en blokkeren bots (captcha/403). De integratie is bewust traag (4–7 s tussen requests per winkel, 6 u interval) en meldt "blocked" per aanbieding in het paneel. Dit is voor persoonlijk gebruik; gebruik een redelijk interval. Wil je stabieler, dan zijn officiële feeds (Amazon PA-API, bol Partner API) een betere bron; de parsers zitten in `parsers.py` en zijn te vervangen.
- Winkel-markup verandert; parsers proberen JSON-LD → meta tags → winkel-specifieke markup. Zie `tests/test_logic.py` voor voorbeelden.
- Één winkel wordt sequentieel bevraagd; bij honderden sets duurt een volledige ronde lang.

## Ontwikkelen
```
# logica-tests (Python 3.11+)
pip install pytest && python -m pytest tests/test_logic.py
# volledige suite met Home Assistant (Python 3.13)
pip install pytest-homeassistant-custom-component home-assistant-frontend && python -m pytest tests
```
