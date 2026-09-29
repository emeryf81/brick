# lot – Lego Organizing Tool

LEGO Price Tracker voor Home Assistant

Custom integration (HACS-compatibel) die LEGO-sets en hun prijzen volgt bij **Amazon.nl, Amazon.de, Amazon.com.be, bol.com en Kruidvat.be**, met een dagelijks dashboard, prijsgrafieken en collectiewaarde.

> Status: 0.4.0. 50 unit- en integratietests tegen een echte Home Assistant-core (2026.2.3). Het paneel is in Chromium getest (desktop en mobiel, licht en donker) met door de integratie zelf gegenereerde testdata. **Nog niet gedaan:** een controle van de winkel-parsers tegen de live sites.

## Opbouw van het paneel
Het sidebar-paneel **LEGO** heeft drie delen:

**🏷️ Deals & watchlist** (sets die je in het oog houdt)
- *Vandaag*: deal van de dag, alle deals (laagste prijs ooit, streefprijs, korting ≥ drempel of dealscore ≥ 70), sets die binnenkort verdwijnen, sterke dalers, en een tijdlijn met recente deals.
- *Watchlist*: sets die je nog niet hebt, sorteerbaar op prioriteit (★–★★★), dealscore, korting, prijs per steen; totaal nu vs. adviesprijs.
- *Alle prijzen*: alles wat gevolgd wordt, met thema- en subthemafilters.

**📦 Mijn collectie** (wat je hebt)
- *Overzicht*: waarde, aankoopkost, groei, stenen, betaald per steen; groeigrafiek; waarde per thema (donut); sets per jaar; grootste stijgers en sets onder aankoopprijs; staat van je sets.
- *Sets*: tegels of tabel, filter op thema en staat (Sealed / Geopend / Gebouwd / Incompleet), locatie, CSV-export.

**⚙️ Beheer**
- *Toevoegen*: kies "in het oog houden" of "in mijn collectie"; meteen melding als de set al gevolgd wordt; bulk plakken; winkel-link koppelen.
- *Importeren*: wizard in drie stappen (bestand → controle → import), zie hieronder.
- *Winkels*: status per winkel (links, met prijs, goedkoopste voor, laatst gelukt, pauze), lijst met mislukte aanbiedingen.
- *Back-up*: JSON-back-up en herstel (samenvoegen of vervangen), CSV-export.

Klik op een set voor de prijsgrafiek per winkel (hover-tooltip, 30 d / 90 d / 1 jaar / alles, lijnen voor advies- en streefprijs), statistieken, handmatige prijs, en het bewerken van *Volgen* (streefprijs, prioriteit, uitfaseerdatum, notitie) en *Collectie* (aantal, betaald, datum, staat, locatie).

Vloeiende animaties (sectiewissel, kaarten, tellende cijfers, grafieken die intekenen, dialoog, meldingen) worden uitgeschakeld als je systeem "minder beweging" vraagt. Het paneel volgt je HA-thema, ook donker.

## Dealscore (0–100)
Korting t.o.v. adviesprijs (max 45) + nabijheid van de laagste prijs ooit (max 25) + korting t.o.v. de mediaan van 90 dagen (max 20) + streefprijs bereikt (10). ≥ 70 = topdeal, ≥ 45 = goede deal.

## Controles
- **CSV-import** wordt eerst volledig gecontroleerd zonder iets op te slaan. Per regel: ongeldig setnummer, aantal 0 of negatief, negatieve prijzen (fout, regel overgeslagen); onwaarschijnlijk jaar/aantal stenen/prijs, prijs > 3× adviesprijs, onbekende of toekomstige datum, datum vóór uitgavejaar, te lange tekst (waarschuwing, veld genegeerd); dubbele regels (samengevoegd als extra exemplaar); al in collectie (wordt bijgewerkt). Max 2 MB / 5000 regels. Nederlandse en Engelse kolomnamen.
- **Bewerken** in het paneel: getallen en datums worden gevalideerd (geen negatieve bedragen, geen toekomstige aankoopdatum, aantal ≥ 1).
- **Prijzen**: een opgehaalde prijs die < 20% of > 4× de adviesprijs is (of sterk afwijkt van de historiek) wordt niet opgeslagen maar als "verdachte prijs" gemeld. Dat vangt accessoires en marketplace-prijzen die per ongeluk worden uitgelezen.
- **Back-up herstellen**: structuur, setnummers, URL's en prijshistoriek worden gecontroleerd vóór er iets verandert.
- **CSV-export** is beschermd tegen formule-injectie in spreadsheets.

## Sensoren, events en services
- **Sensoren**: gevolgde sets, sets op laagste prijs ooit, sets met hoge korting, sets onder streefprijs, beste dealscore (top 5 in de attributen), sets die binnenkort verdwijnen, wishlist-kost, aanbiedingen met fout, collectiewaarde/-kost/-groei, plus één prijssensor per set (prijs per steen, trend, dealscore, prijzen per winkel als attributen).
- **Events**: `lego_tracker_new_all_time_low`, `lego_tracker_high_discount`, `lego_tracker_target_price_reached`, `lego_tracker_daily_digest`. Kies in de opties een notify-service voor pushmeldingen.
- **Services**: `add_set`, `add_sets`, `remove_set`, `set_offer`, `report_price`, `discover_offers`, `refresh`, `import_collection`, `export_collection`, `export_data`, `import_data`, `send_digest`.

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
