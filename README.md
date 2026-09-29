# lot – Lego Organizing Tool

<img width="1024" height="559" alt="afbeelding" src="https://github.com/user-attachments/assets/fe8aea1b-0443-4675-8136-d3f0b021fe98" />

buymeacoffee:emeryf

LEGO Price Tracker voor Home Assistant

Custom integration (HACS-compatibel) die LEGO-sets en hun prijzen volgt bij **LEGO.com, Amazon.nl, Amazon.de, Amazon.com.be, bol.com, Kruidvat.be, Dreamland.be en je eigen winkels**, met een dagelijks dashboard, prijsgrafieken en collectiewaarde.

> Status: 0.8.0. 78 unit- en integratietests tegen een echte Home Assistant-core (2026.2.3). Het paneel is in Chromium getest (desktop en mobiel, licht en donker) met door de integratie zelf gegenereerde testdata. **Nog niet gedaan:** een controle van de winkel-parsers tegen de live sites.

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

## Knoppen, taken en schema
Bovenaan staan altijd **🔎 Links zoeken** en **↻ Prijzen verversen**; onder *Beheer → Winkels & schema* staat ook **ℹ️ Setgegevens aanvullen**. Elke knop start een **achtergrondtaak**:
- Er loopt altijd maar één taak tegelijk. Een balk bovenaan toont de voortgang (x/y, resterende tijd, huidige set), en je kunt de taak stoppen.
- Winkels worden per set **parallel** bevraagd (elke winkel apart nog steeds rustig), dus een ronde gaat ongeveer 5× sneller dan voorheen.
- **Gepauzeerde winkels** (na een blokkade) worden overgeslagen zonder hun laatste prijs te wissen. Bij het starten kun je kiezen om het toch te proberen.
- Na afloop volgt een melding en het event `lego_tracker_job_finished`.

**Automatisch ophalen**: standaard om `07:30, 19:30`. Aan/uit en tijdstippen (1 tot 6) stel je in via *Instellingen → Apparaten & diensten → LEGO Price Tracker → Configureren*. Bij het opstarten van Home Assistant wordt niet meer automatisch alles opgehaald; dat leidde tot blokkades.

**Setgegevens** (naam, thema, subthema, jaar, stenen, afbeelding, adviesprijs, uitfaseerdatum) komen uit de Brickset-API (sleutel), de Rebrickable-API (gratis sleutel via rebrickable.com → Account → API) of, zonder sleutel, de openbare Brickset-pagina. Namen die van een verkeerd winkelproduct kwamen, worden vervangen. Namen uit je import of die je zelf invulde blijven staan.

## LEGO.com als eerste bron
Voor **adviesprijs, afbeelding en naam** is LEGO.com de eerste bron. De integratie zoekt de officiële productpagina (land instelbaar, standaard `nl-be`), neemt daar de gewone prijs als adviesprijs (ook als er een actie loopt), de officiële afbeelding en de naam, en volgt de pagina als winkel **LEGO.com**. Een actie op LEGO.com telt dus mee als deal. Staat de set daar als "binnenkort niet meer verkrijgbaar", dan krijgt ze de markering *verdwijnt binnenkort*. Thema, jaar en stenen komen daarna uit Brickset/Rebrickable. Een adviesprijs of afbeelding die je zelf invult, wordt nooit overschreven.

Nog niet tegen de live site gecontroleerd: LEGO.com rendert een deel van de zoekpagina in de browser. Vindt "Links zoeken" niets, dan probeert de integratie de productpagina rechtstreeks (`/nl-be/product/<setnummer>`). Je kunt de juiste LEGO.com-link ook zelf koppelen.

**Opgeruimde prijzen:** prijspunten die onmogelijk zijn ten opzichte van de adviesprijs (< 20 % of > 4×), bijvoorbeeld een accessoireprijs die verkeerd werd uitgelezen, worden automatisch uit de historiek verwijderd.

## Logboek
*Beheer → Logboek* bewaart alles wat de integratie doet (laatste 3000 regels):
- prijswijzigingen (oud → nieuw) per winkel;
- verbindingen met winkels, met per taak een samenvatting per winkel ("bol.com: 230 gelukt, 6 mislukt");
- fouten (geblokkeerd, pagina weg, prijs niet gevonden, verdachte prijs), pauzes en hervattingen;
- gevonden, goedgekeurde, afgekeurde en verwijderde links;
- prijzen via het userscript (Tampermonkey), ook de geweigerde;
- imports, taken, setgegevens (LEGO.com/Brickset/Rebrickable), meldingen, instellingen en je eigen acties.

Filteren kan op fouten/gebeurtenissen, soort, winkel, bron (server, schema, paneel, Tampermonkey, import…), setnummer en tekst of link. Identieke fouten na elkaar worden samengevoegd (×aantal). Klik een regel open voor details; bij een winkel- of linkfout vul je meteen de juiste link en/of de prijs in (**Rechtzetten**). *Openstaande fouten* toont alle huidige problemen, met dezelfde herstelknop, en je kunt ook opnieuw proberen, negeren of de link verwijderen.

## Notificaties
*Beheer → Notificaties* werkt met regels, bijna volledig via dropdowns:
1. **Voor welke sets**: alle, watchlist, mijn collectie, bepaalde thema's, of bepaalde sets (eerst een thema kiezen, dan de set, of vrij een setnummer typen).
2. **Wanneer**: laagste prijs ooit, korting ≥ x %, streefprijs bereikt, prijs onder € x, prijsdaling ≥ x %, dealscore ≥ x, verdwijnt binnenkort, weer leverbaar, elke prijswijziging; algemeen: dagelijkse samenvatting, taak klaar, problemen (winkel gepauzeerd, fouten). Optioneel beperkt tot bepaalde winkels.
3. **Naar wie en hoe**: 📱 Home Assistant-app (toestel kiezen, met afbeelding en klikbare link), ✉️ e-mail (via SMTP of een andere notify-dienst, vrije adressen), 💬 elke notify-service (Telegram, Signal…), 📣 notify-entiteiten, 🔔 melding in Home Assistant, 🔊 spraak op een speaker (TTS + mediaspeler), ⚡ alleen het event `lego_tracker_notification`.
4. **Extra**: niet opnieuw melden binnen x uur, stille uren (meldingen worden gebundeld en daarna verstuurd), afbeelding en link meesturen.

Elke regel heeft een testknop; verstuurde meldingen staan in het logboek. Bij een update worden twee standaardregels aangemaakt (alle deals en de dagelijkse samenvatting), naar de notify-service uit je oude instellingen als die er was.

## Instellingen in het paneel
Alles staat onder *Beheer → Instellingen* (alleen voor beheerders); de integratie-opties in Home Assistant blijven ook werken:
- kortingsdrempel, historiek, tijdstip van de samenvatting (meldingen zelf: zie Notificaties);
- automatisch ophalen aan/uit en tijdstippen;
- **collectiewaarde**: eerst winkelprijs, of eerst de geïmporteerde waarde (bv. BrickEconomy);
- **API-sleutels** voor Brickset en Rebrickable, met een testknop. Sleutels worden nooit terug naar de browser gestuurd (alleen `••••1234`). Volgorde: Brickset → Rebrickable → openbare Brickset-pagina; wat de ene bron mist of niet levert, vult de volgende aan;
- **winkels**: aan/uit, pauze opheffen, *automatisch pauzeren* per winkel aan/uit, en voor **elke** winkel de zoek-URL (al ingevuld, ↺ zet de standaard terug; `{query}`, `{number}`, `{locale}`); LEGO.com-land;
- **eigen winkel toevoegen**: naam, domein en een zoek-URL met `{query}`. Prijzen worden gelezen uit de standaard productgegevens (JSON-LD/meta) die de meeste webwinkels hebben.

Pauzes blijven bewaard bij een herstart, zodat een herstart een winkel die net blokkeerde niet opnieuw bestookt.

**Dreamland** is ingebouwd en wordt bij een update eenmalig aangezet. De zoek-URL (`https://www.dreamland.be/e/nl/search?q={query}`) is nog niet tegen de echte site gecontroleerd; pas hem aan in de instellingen als het zoeken niets vindt.

## Userscript (prijzen vanuit je eigen browser)
Onder *Beheer → Userscript* staat de installatie in vijf stappen, met een link naar Tampermonkey voor jouw browser. Home Assistant maakt het script zelf aan op `/api/lego_tracker/lego-tracker.user.js`, met jouw HA-adres al ingevuld en alle winkels van je lijst. Het token vul je in via Tampermonkey (niet in het script). Het script stuurt ook de producttitel mee, zodat de linkcontrole Amazon-links kan beoordelen. Het tabblad toont wanneer de laatste prijs via het script binnenkwam.

## Collectie bijwerken via CSV
Lees je nieuwe export opnieuw in bij *Beheer → Importeren*. Bestaande sets worden bijgewerkt (aantal, betaald, **huidige waarde**, staat, locatie), nieuwe sets worden toegevoegd. Met *daarna bijwerken* (standaard aan) start een taak die per set de gegevens aanvult, ontbrekende winkellinks zoekt en de prijzen ophaalt. Elke gewijzigde waarde wordt bewaard als historiek, zodat de groeigrafiek je herhaalde imports volgt. De collectietabel toont *Waarde (import)*, *Winkel nu* en de *Waarde* die meetelt.

## Linkcontrole
*Beheer → Linkcontrole* beoordeelt elke winkellink:
- **✓ klopt**: het setnummer staat in de producttitel (of de URL), met "LEGO" erbij.
- **⚠ verdacht**: het is een accessoire (verlichting, vitrine, stofkap, stickers…), een namaakmerk (Keeppley, Mould King, Cada…), het setnummer ontbreekt, of de prijs is veel te laag voor de set. Ook sets waarvan de naam van een verkeerd product kwam, worden gemarkeerd.
- **? niet gecontroleerd**: nog geen titel bekend. Voor Amazon komt die bij de volgende prijsronde.

Verdachte links **tellen niet mee** voor prijzen, laagste prijs ooit en collectiewaarde. Per link kun je ✓ goedkeuren, ✎ een andere URL/ASIN invullen (telt meteen als goedgekeurd) of 🗑 verwijderen. Een verwijderde link wordt nooit opnieuw automatisch gekoppeld. Het zoeken naar links gebruikt dezelfde controle, zodat foute producten niet meer worden gekozen.

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
- **Services**: `add_set`, `add_sets`, `remove_set`, `set_offer`, `report_price`, `discover_offers`, `refresh` (beide met `force`), `enrich_sets`, `import_collection` (met `update_after`), `verify_links`, `confirm_offer`, `remove_offer`, `cancel_job`, `import_collection`, `export_collection`, `export_data`, `import_data`, `send_digest`.

## Installatie
1. HACS → Custom repositories → deze repo, categorie *Integration* (of kopieer `custom_components/lego_tracker` naar je `config/`).
2. Herstart HA → Instellingen → Apparaten & diensten → *LEGO Price Tracker* toevoegen.
3. Opties: kortingsdrempel, winkels, automatisch ophalen + tijdstippen (standaard 07:30 en 19:30), uur van de samenvatting, optioneel een Rebrickable-sleutel, optioneel een gratis [Brickset API-sleutel](https://brickset.com/tools/webservices/requestkey) voor naam/thema/adviesprijs/afbeelding.

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
   - **Userscript** (Tampermonkey), te installeren via *Beheer → Userscript*: draait in *jouw eigen browser* op de productpagina's die je bezoekt en stuurt prijs en titel naar HA.
   - **Service `lego_tracker.report_price`** (`price` + `url`, of `price` + `set_number` + `retailer`): voor n8n, een automation of een ander script.
   - **Handmatig** in het detailvenster van een set (veld "Handmatig").

Betere bronnen dan scrapen, als het blijft haperen: bol.com Marketing Catalog API (gratis partneraccount) en Amazon PA-API (vereist een partneraccount met verkopen). Die zijn nog niet ingebouwd, want ik kon ze niet verifiëren; `parsers.py` en `client.py` zijn de plek om ze in te pluggen.

## Overige beperkingen
- Amazon en bol.com **staan scraping niet toe** in hun voorwaarden. Gebruik het voor persoonlijk gebruik met een redelijk interval (standaard twee rondes per dag, 4–7 s tussen requests per winkel).
- Winkel-markup verandert; parsers proberen JSON-LD → meta tags → winkel-specifieke markup. Zie `tests/test_logic.py`.
- Eén winkel wordt sequentieel bevraagd; bij honderden sets duurt een volledige ronde lang.

## Ontwikkelen
```
# logica-tests (Python 3.11+)
pip install pytest && python -m pytest tests/test_logic.py
# volledige suite met Home Assistant (Python 3.13)
pip install pytest-homeassistant-custom-component home-assistant-frontend && python -m pytest tests
```
