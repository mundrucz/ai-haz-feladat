# Számlafeldolgozó és cash-flow kimutatás

A Nordwind Solutions Kft. számára készült webalkalmazás. PDF-számlákat tölt fel, LLM-mel kinyeri belőlük az adatokat, majd ellenőrzés után eldönti az irányt (bejövő vagy kimenő) és a partnert. Az összegeket a teljesítés napjára érvényes MNB-árfolyamon forintra váltja, és egy oldalon havi cash-flow diagramot, cégenkénti összesítőt és számlalistát mutat.

## Indítás

```
cp .env.example .env        # Windows: Copy-Item .env.example .env
# a .env-be írd be az OPENROUTER_API_KEY értékét
docker compose up --build
```

Elérés: **http://localhost:8080** (a port az `APP_PORT` változóval állítható). Docker nélkül semmit nem kell telepíteni.

- A `.env` kötelező: nélküle a `docker compose` nem indul el. A `.env` a gitignore-ban van, a kulcs nem kerül a repositoryba.
- Az `OPENROUTER_API_KEY` kötelező: üres vagy hiányzó kulccsal az alkalmazás érthető hibával (`invalid or missing settings: OPENROUTER_API_KEY …`) nem indul el, mert kinyerés nélkül egyetlen számla sem dolgozható fel.
- Leállítás: `docker compose down`. Az adatok (adatbázis, hibanapló) az `app-data` volume-on maradnak. Teljes törlésük: `docker compose down -v`.

## Konfiguráció

Minden beállítás környezeti változóból (`.env`) jön; az alapértékeket és a magyarázatokat a `.env.example` tartalmazza.

| Változó | Alapérték | Szerep |
|---|---|---|
| `APP_PORT` | `8080` | a gépen ezen a porton érhető el az alkalmazás |
| `OPENROUTER_API_KEY` | **kötelező** | LLM-kulcs; üresen az alkalmazás nem indul el |
| `OPENROUTER_MODEL` | `openai/gpt-4o-mini` | az LLM-modell neve |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-kompatibilis végpont |
| `LLM_TIMEOUT_SECONDS`, `LLM_CONCURRENCY` | `60`, `4` | LLM-időkorlát, párhuzamos hívások száma |
| `COMPANY_NAME`, `COMPANY_TAX_NUMBER`, `COMPANY_EU_VAT` | **kötelező** | a saját cég; ez alapján dől el az irány |
| `MNB_URL`, `MNB_TIMEOUT_SECONDS`, `MNB_CONCURRENCY` | `http://www.mnb.hu/arfolyamok.asmx`, `20`, `2` | MNB SOAP-végpont (csak `http`-n működik) |
| `MNB_LOOKBACK_DAYS` | `31` | legfeljebb ennyi nappal korábbi jegyzést fogadunk el |
| `MNB_CURRENCIES_TTL_DAYS` | `7` | ennyi napig használjuk a tárolt MNB-devizalistát |
| `REPORT_CURRENCY` | `HUF` | a kimutatás pénzneme |
| `DEFAULT_LOCALE` | `hu` | a felület nyelve; a szám- és dátumformátum is ehhez igazodik |
| `MAX_UPLOAD_MB` | `20` | egy fájl legnagyobb mérete |
| `PROCESSING_CONCURRENCY`, `PROCESSING_QUEUE_SIZE` | `4`, `8` | feldolgozó munkások száma, a sor hossza |

- A saját cég adatainak szándékosan nincs alapértéke: egy rossz alapérték csendben rossz irányt adna minden számlának. Hiányukban az alkalmazás érthető hibával nem indul el.
- Ha egy technikai beállítás érvénytelen (például szám helyett szöveg), az alapértéke lép életbe, és ezt induláskor figyelmeztetés jelzi (`invalid setting …, using default …`).
- Az adatbázis útvonala Dockerben rögzített (`/app/data/app.db`, a volume-on).

## Architektúra

FastAPI-alkalmazás, Jinja2-oldallal és JSON API-val, egyetlen konténerben és egyetlen uvicorn-folyamatban. Az adatok SQLite-ban vannak (SQLAlchemy 2 async ORM, `aiosqlite`, Alembic-migrációk). Minden I/O aszinkron; a PDF-szöveg kinyerése (CPU-igényes) szálkészletben fut (`asyncio.to_thread`).

Rétegek: a felső réteg használja az alsót, fordítva soha.

```
api  →  services  →  repositories, clients, extraction  →  db, core
```

```
app/
  main.py         composition root: itt jön létre és kapcsolódik össze minden objektum (máshol nincs példányosítás)
  core/           config.py (beállítások), errors.py (kivételhierarchia), i18n.py, logging_config.py
  db/             database.py (kapcsolat, tranzakció, induláskori migrálás), tables.py (ORM-modellek), migrations/
  repositories/   invoice_repository.py, rate_repository.py: csak tárolás, üzleti döntés nélkül
  clients/        llm_client.py (OpenRouter), mnb_client.py (MNB SOAP): HTTP, időkorlát, újrapróbálás, hibatípusok
  extraction/     llm.py (prompt, válaszfeldolgozás), grounding.py, validation.py, line_items.py, normalize.py,
                  company.py, labels.py, lexicon.py; adatfájlok: resources/languages/*.json, resources/currencies.json
  services/       üzleti logika (lásd lent)
  api/            végpontok üzleti logika nélkül: document_routes, invoice_routes, report_routes, page_routes
  web/            templates/ (Jinja2), static/ (app.js, CSS, vendorolt Chart.js), locales/ (hu.json)
tests/            ugyanilyen almappákban (core, db, clients, services, extraction, api)
```

A fájlnév utótagja jelzi a réteget (`*_service.py`, `*_repository.py`, `*_client.py`, `*_routes.py`). A függőségeket konstruktorban kapják (HTTP-kliens, repository, óra, beállítás), a cserélhető részek `Protocol` mögött vannak, így mockolhatók.

### Service-ek

| Service | Feladat |
|---|---|
| `UploadService` (`upload_service.py`) | fogadja a fájlt; üres, túl nagy és duplikált (SHA-256) fájl kiszűrése, majd sorba állítás |
| `JobTracker` (`job_service.py`) | a folyamatban lévő és a hibás fájlok állapota a memóriában; ez a feldolgozási lista forrása |
| `DocumentProcessor` (`processing_service.py`) | korlátos sor és `PROCESSING_CONCURRENCY` háttérmunkás; egy fájl hibája csak azt a fájlt érinti |
| `pdf_service.py` | PDF-szöveg oldalanként (PyMuPDF); nem PDF, sérült, jelszavas és szöveg nélküli (szkennelt) fájl felismerése |
| `InvoiceAnalyzer` (`invoice_service.py`) | LLM-kinyerés → kötelező mezők → validáció → irány és partner → devizaellenőrzés → mentés |
| `RateService` (`rate_service.py`) | árfolyam-kiválasztás, hiányzó időszakok lekérése, single-flight, átváltás |
| `ReportService` (`report_service.py`) | forintra váltás, havi bontás, partnerösszesítő, számlalista |
| `FailedFileLog` (`failed_file_service.py`) | a hibás fájlok tartós naplója (JSON Lines) |

### Végpontok

| Végpont | Szerep |
|---|---|
| `GET /` | az egyoldalas felület |
| `POST /api/documents` | PDF-ek feltöltése (multipart, több fájl) |
| `GET /api/documents` | folyamatban lévő és hibás fájlok |
| `POST /api/documents/uploads/{id}/cancel` | egy feltöltési menet leállítása |
| `DELETE /api/documents/failed` | a hibás fájlok listájának ürítése |
| `GET /api/invoices`, `DELETE /api/invoices` | tárolt számlák listája, illetve mindegyik törlése |
| `GET /api/report` | a teljes kimutatás (diagram, összesítő, számlalista) |
| `GET /health` | állapot (Docker healthcheck) |

## A feldolgozás menete

1. **Feltöltés.** A böngésző a kijelölt fájlokat 10-esével küldi. A szerver ellenőrzi a fájlt (üres, túl nagy, már tárolt vagy éppen feldolgozott tartalom-hash), majd a feldolgozási sorba teszi. A sor korlátos (`PROCESSING_QUEUE_SIZE`): ha megtelt, a feltöltési kérés vár, így a böngésző következő csomagja is késik. A memóriában tartott PDF-ek száma ezért nem nő a fájlok számával. A feltöltés nem várja meg a feldolgozást.
2. **PDF-szöveg.** A munkás kinyeri a szöveget (minden oldalt). A PDF csak eddig él a memóriában, nem tároljuk.
3. **LLM-kinyerés.** Egy hívás fájlonként (`temperature: 0`, JSON-válasz). A modell ISO-dátumot, ISO 4217 devizakódot és pontos tizedesű összeget ad vissza.
4. **Ellenőrzés.** Grounding, kötelező mezők, validáció (lásd „Az LLM-válasz ellenőrzése”).
5. **Irány és partner.** Kimenő, ha a kiállító a saját cég; bejövő, ha a vevő. A partner a másik fél.
6. **Devizaellenőrzés.** Jegyzi-e az MNB a számla devizáját (`GetCurrencies`).
7. **Mentés.** A kinyert mezők, az irány, a partner, az állapot (`ok` vagy `needs_review`) és a figyelmeztetések kerülnek az adatbázisba.
8. **Kimutatás.** A felület a `GET /api/report` végpontot kérdezi. Ilyenkor kerül sor az árfolyamok lekérésére (csak a hiányzókat), a forintra váltásra és az összesítésre.

A felület feltöltés közben 1,5 másodpercenként lekérdezi a feldolgozási listát és a kimutatást, és újrarajzolja őket; ha nincs folyamatban lévő munka, a lekérdezés leáll. Az eredmény az adatbázisban van, így oldalfrissítés és újraindítás után is megmarad.

A **Leállítás** gomb után a már futó fájlok befejeződnek. A sorban állók és a még fel sem küldöttek nyom nélkül kimaradnak: nem kerülnek a hibás fájlok közé, csak a böngésző összegzése jelzi a számukat, és később újra feltölthetők.

## Adatkinyerés

A mezőket az LLM nyeri ki a PDF szöveges rétegéből: számlaszám, a kiállító és a vevő neve és adószáma, a kiállítás, a teljesítés és a fizetési határidő dátuma, a pénznem, a bruttó végösszeg, valamint (opcionálisan) a nettó és az ÁFA. Az irányt és a partnert nem az LLM adja, hanem a kód dönti el a saját cég adataiból.

**Miért LLM, és nem szabályalapú kinyerés?** A számlák nyelve, elrendezése, dátum- és számformátuma tetszőleges, a rejtett tesztszámlák pedig eltérnek a mintáktól. A címkékre és pozíciókra épülő szabályok csak a látott formákon működnek megbízhatóan. Az LLM bármely nyelvet és elrendezést kezel; a gyengeségét (kitalált vagy összekevert érték) a kód ellenőrzésekkel fogja meg.

**Ismeretlen formátum, nyelv, pénznem:**

- A kódban nincs nyelv- vagy devizafelsorolás, és nincs nyelvenkénti ág. Az LLM bármely nyelvű számlát feldolgoz, és bármely ISO 4217 kódot visszaadhat.
- Az ellenőrzésekhez használt adatfájlok csak a felvett nyelveket és devizákat ismerik (jelenleg a feladat nyelveit és devizáit):
  - `resources/languages/*.json`: hónapnevek (a dátum összevetéséhez) és az ÁFA-összesen címkéi (az ÁFA-kulcs ellenőrzéséhez);
  - `resources/currencies.json`: devizajelek és -nevek (a pénznem összevetéséhez).

  Új nyelv vagy deviza: új vagy bővített adatfájl, kódmódosítás nélkül. Ami nincs felvéve (például holland hónapnév vagy ismeretlen devizajel), az kinyerhető marad, csak az ellenőrzés gyengébb.
- A számok tizedes- és ezreselválasztóját a szám szerkezete határozza meg (`1 234,56`, `1.234,56`, `1,234.56`, `1'234.56`), nem a nyelv.
- A többoldalas számla minden oldala bekerül a szövegbe (legfeljebb 40 000 karakter).
- Ha a modell nem talál számlát, vagy hiányzik egy kötelező mező, a fájl érthető hibával a hibás fájlok közé kerül, és nem tárolódik.

## LLM-modell és indoklás

Alapértelmezés: **`openai/gpt-4o-mini`** OpenRouteren keresztül; a modell az `OPENROUTER_MODEL` változóval cserélhető.

- A kapott kulcs fiókja csak zero data retention (ZDR) végpontot enged. Az eredetileg tervezett `google/gemini-2.5-flash` ott nem érhető el, a `gpt-4o-mini` igen.
- Olcsó és gyors, megbízhatóan ad JSON-t (`response_format: json_object`), és a feladat (mezők kimásolása a szövegből) nem igényel erős következtetést.
- A rossz válasz kockázatát nem nagyobb modell, hanem a kódbeli ellenőrzés csökkenti.

A kliens csak átmeneti hibánál próbálkozik újra (lásd „Hibakezelés”), a hibakódokat a „Hibakódok” szakasz sorolja fel. A promptban a dokumentum szövege megbízhatatlan adatként van jelölve, a benne lévő utasításokat a modellnek figyelmen kívül kell hagynia.

## Az LLM-válasz ellenőrzése

Az LLM válaszát nem fogadjuk el vakon. A lépések sorrendben:

1. **Séma és típus.** A válasz JSON-objektum; a dátum érvényes ISO-dátum, az összeg `Decimal`, a pénznem háromjegyű kód. Az érvénytelen érték üresnek számít.
2. **Grounding.** Minden visszaadott értéknek szerepelnie kell a dokumentumban:
   - a számlaszám és az adószám tagolástól függetlenül (az adószám országelőtag nélkül is);
   - a név szavai;
   - a dátum bármely numerikus vagy hónapneves alakban;
   - az összeg bármely tagolással;
   - a pénznem kódként, jelként vagy névként.

   Ha egy érték nincs a szövegben, a fájl `llm_invalid_response` hibát kap az érintett mezőkkel.
3. **Kötelező mezők.** Számlaszám, a felek neve és adószáma, a három dátum, a pénznem és a bruttó. Ha egy hiányzik: `incomplete_data`, a számla nem tárolódik. Ha semmi nincs: `no_invoice_found`. A nettó és az ÁFA hiánya nem hiba. A teljesítés dátumára nincs tartalék, mert ez az árfolyam napja.
4. **Validáció.** Érvénytelen adatnál (például nem létező devizakód) a fájl hibás; gyanús adatnál a számla ellenőrzendőként (`needs_review`) tárolódik, a számlalistában jelölve látszik, de a diagramba és a cégenkénti összesítőbe nem kerül. A sztornó számla (negatív bruttó) nem gyanús: az összeg-ellenőrzések az ellentett értékekkel futnak.
   - `net_exceeds_gross`, `totals_mismatch`: a nettó nagyobb a bruttónál, vagy nettó + ÁFA ≠ bruttó (tűréssel);
   - `vat_rate_mismatch`: az ÁFA nem egyezik a számlán szereplő ÁFA-kulccsal;
   - `line_items_mismatch`: a tételsorok (mennyiség × egységár = sorösszeg) összege nem egyezik a nettóval vagy a bruttóval; tételsor nélkül nincs ítélet;
   - `due_before_issue`, `performance_after_due`, `date_out_of_range`: a dátumok sorrendje vagy tartománya gyanús;
   - `zero_total`, `same_party`, `company_not_found`: nulla végösszeg, azonos kiállító és vevő, illetve a saját cég nem szerepel a számlán;
   - `no_direction`: a saját cég se kiállító, se vevő (vagy mindkettő);
   - `currency_not_quoted`: az MNB nem jegyzi a devizát.

## Irány, partner, duplikáció

- **Saját cég felismerése:** a kinyert fél adószáma vagy neve (tagolás- és kisbetű-függetlenül) egyezik a beállított adószámmal, EU-adószámmal vagy névvel. Irány nélküli számla tárolódik, de kimarad az összesítésből: nem találgatunk, mert a rossz irányt nehezebb észrevenni, mint egy jelölt, kimaradt számlát.
- **Partner:** az adószám azonosítja. A magyar formát egységesítjük (a `HU` előtag és a 11 jegyű adószám első 8 jegye egyezik); a megjelenített név a partner leggyakoribb neve.
- **Duplikáció, két szinten, az adatbázis egyedi kulcsaival is kikényszerítve:**
  - ugyanaz a fájl: a tartalom SHA-256 hash-e (`content_hash` UNIQUE). Az újrafeltöltés idempotens, nem hív újra LLM-et; a szerver már a feltöltéskor kiszűri, a folyamatban lévő fájlokkal szemben is;
  - ugyanaz a számla más fájlban: a kiállító normalizált adószáma és a számlaszám (`identity_key` UNIQUE), `duplicate_invoice` hibával. A mentés író zár alatt fut (`BEGIN IMMEDIATE`), az esetleges egyedikulcs-ütközést is duplikátumként kezeljük, így párhuzamos feltöltésnél sincs kettőzés.

## MNB-árfolyamok lekérése és tárolása

- **Melyik árfolyam:** a teljesítés napjának MNB-árfolyama; ha aznap nem volt jegyzés (hétvége, ünnepnap), az utolsó megelőző jegyzés, legfeljebb `MNB_LOOKBACK_DAYS` (31) napra visszamenőleg. Az `unit` mezőt figyelembe vesszük (például 100 JPY).
- **Jövőbeli teljesítés:** a mai nap legutóbbi jegyzését kapja (becsült árfolyam). A felület jelöli, hogy az összeg nem végleges; a teljesítés napja után a forintösszeg magától a valódi árfolyamra vált.
- **Átváltás:** `bruttó × árfolyam / unit`, `Decimal`-lal, számlánként egész forintra kerekítve (`ROUND_HALF_UP`). Az összesítés csak ezután történik. A kimutatás pénznemében kiállított számlát nem váltjuk át.
- **Kliens:** nyers SOAP `httpx`-szel és `xml.etree`-vel (`GetExchangeRates`, `GetCurrencies`). Nincs beégetett deviza és nincs beégetett céldeviza. A támogatott devizák körét az MNB `GetCurrencies` válasza adja.
- **Tárolás (SQLite):**
  - `exchange_rates`: deviza + nap, egyedi kulccsal;
  - `rate_coverage`: mely időszakok vannak már lekérve devizánként, így a jegyzés nélküli napokat sem kérdezzük újra;
  - `mnb_currencies`: a jegyzett devizák listája a lekérés napjával; 7 napig érvényes, utána újra lekérjük.
- **Lekérés:**
  - A kimutatás előtt összegyűjtjük, mely devizákra és időszakokra van szükség, és csak a hiányzó részeket kérjük le.
  - A hiányzó devizák egy `GetExchangeRates` kérésben, tartományként mennek. Egy kérés legfeljebb 366 napot fed; a hosszabb tartomány ilyen darabokra bomlik, a hiány nélküli darab kimarad.
  - Egyszerre egy lekérés fut (single-flight): a várakozó hívás a zár után újra megnézi, mi hiányzik még.
  - A mai napot nem jegyezzük lekértnek, mert a napi árfolyam napközben még hiányozhat.
  - Ha az MNB egy devizát elutasít, a devizák külön kérésben mennek, hogy a többi ne vesszen el.
- **Kiesés:**
  - Elérhetetlen MNB-t legfeljebb óránként próbálunk újra, addig a legutóbbi hibát adjuk vissza.
  - A már biztosan tárolt árfolyamú számlák kiesés alatt is átváltódnak; a többi a számlalistában hibakóddal látszik (`mnb_unavailable`, `mnb_no_rate`), és kimarad az összesítésből.
  - Az MNB visszatérése után elég frissíteni az oldalt.

## Kimutatás

Egy oldalon, ebben a sorrendben; minden összeg bruttó, a kimutatás pénznemében (alapértelmezés: HUF).

1. **Havi cash-flow vonaldiagram** (Chart.js, a repóban vendorolva): a fizetési határidő hónapja szerint, bejövő és kimenő vonallal. Az üres hónapok 0 értéket kapnak; jelmagyarázattal, tengelyfeliratokkal és ezres tagolással.
2. **Cégenkénti összesítő:** oszlopban a partnerek, sorban az Össz bejövő és az Össz kimenő; a hiányzó irány értéke 0.
3. **Számlalista:** az eredeti összeg és pénznem, a felhasznált MNB-árfolyam (egységgel) és annak dátuma, a forintösszeg, az irány, a partner és az állapot. Az ellenőrzendő vagy át nem váltható számla oka is látszik. A lista csoportonként szűrhető: összes kinyert, sztornó, jövőbeli teljesítésű, ellenőrzendő; a sztornó és a becsült árfolyamú számla címkét kap.

**Mi kerül a diagramba és az összesítőbe:** a rendben lévő, irányos, átváltott számla, köztük a sztornó (negatív összeggel, csökkenti a partner összegét) és a jövőbeli teljesítésű (mai árfolyamon becsülve). Kimarad az ellenőrzendő (`needs_review`), az árfolyam nélküli és az irány nélküli számla. A diagram és az összesítő alatt ezt darabszámokkal kiírjuk, a becsült árfolyamú számlákra külön figyelmeztetéssel. Fölötte a feldolgozási lista mutatja a folyamatban lévő és a hibás fájlokat, fájlonként érthető okkal. A felület szövegei a `web/locales/hu.json`-ból jönnek; a szám- és dátumformátum a `DEFAULT_LOCALE`-hoz igazodik.

## Hibakezelés és naplózás

- **Semmilyen bemenet vagy külső hiba nem állítja le az alkalmazást.** Saját kivételhierarchia van (`AppError` → `ProcessingError` stb., mindegyik hibakóddal). A határokon (API, háttérmunkás) kapjuk el és naplózzuk; a hiba csak az érintett fájlt vagy számlát érinti. A kódok listája: „Hibakódok”.
- **Újrapróbálás:** az MNB- és az LLM-kliens csak átmeneti hibánál próbálkozik újra: időtúllépés, megszakadt kapcsolat, illetve 408, 425, 429, 500, 502, 503 és 504-es státusz. Legfeljebb 2 újrapróbálás van, 1, majd 2 mp várakozással. A végleges hibát (400, 401 rossz kulcs, 402 elfogyott keret, 404, SOAP-hiba) azonnal jelezzük. A hibás fájl újrafeltöltéssel próbálható újra.
- **Konzolnapló:** `docker compose logs app`. Indítás, beállítás-figyelmeztetések, hibák stack trace-szel; titkok nélkül.
- **Hibás fájlok naplója:** `/app/data/logs/failed_files.log`, a volume-on, így újraindítás után is megmarad. Egy sor egy JSON-objektum (JSON Lines), a következő mezőkkel: `source` (`intake`, `processing`, `report`), `file_name`, `error_code`, `error_text`, `error_fields`, `content_hash`, `upload_id`, `job_id`, `invoice_id` stb. A fájl 5 MB-onként forog, 3 régi példány marad meg. Megtekintés: `docker compose exec app cat /app/data/logs/failed_files.log`.

## Hibakódok

Minden hiba kódot és a nyelvi fájlból (`web/locales/hu.json`) fordított üzenetet kap; a naplókban a kód szerepel.

**Fájlhibák.** A fájl a „Hibás fájlok” listába kerül, a számla nem tárolódik. Újrafeltöltéssel próbálható újra.

| Kód | Csoport | Jelentés |
|---|---|---|
| `empty_file` | feltöltés | üres fájl |
| `file_too_large` | feltöltés | nagyobb, mint `MAX_UPLOAD_MB` |
| `duplicate_file` | feltöltés | azonos tartalmú fájl már tárolva van, vagy éppen feldolgozás alatt áll |
| `not_a_pdf` | PDF | a fájl nem PDF |
| `corrupt_pdf` | PDF | sérült, nem nyitható meg |
| `password_protected` | PDF | jelszóval védett |
| `no_text` | PDF | nincs szöveges réteg (üres vagy szkennelt dokumentum) |
| `no_invoice_found` | kinyerés | az LLM egyetlen számlamezőt sem talált (nem számla) |
| `incomplete_data` | kinyerés | hiányzik egy kötelező mező; az érintett mezők listázva |
| `invalid_data` | kinyerés | érvénytelen érték, például nem létező devizakód |
| `duplicate_invoice` | kinyerés | ugyanez a számla (kiállító adószáma + számlaszám) már tárolva van másik fájlból |
| `llm_auth` | LLM | érvénytelen kulcs vagy hiányzó jogosultság (401, 403) |
| `llm_quota` | LLM | elfogyott a keret (402) |
| `llm_rate_limited` | LLM | kérésszám-korlát (429), az újrapróbálások után is |
| `llm_unavailable` | LLM | időtúllépés, megszakadt kapcsolat vagy szerverhiba (5xx), az újrapróbálások után is |
| `llm_model_unavailable` | LLM | rossz modellnév vagy végpont (404; `OPENROUTER_MODEL`, `OPENROUTER_BASE_URL`) |
| `llm_invalid_response` | LLM | a válasz nem JSON, eltér a sémától, vagy egy értéke nem szerepel a dokumentumban (grounding) |
| `llm_error` | LLM | egyéb sikertelen hívás (például 400) |
| `unexpected_error` | egyéb | váratlan hiba; a részletek a konzolnaplóban |

**Ellenőrzendő számla (`needs_review`).** A számla tárolódik, a számlalistában jelölve látszik, de kimarad a diagramból és a cégenkénti összesítőből. A részletek: „Az LLM-válasz ellenőrzése”.

| Kód | Jelentés |
|---|---|
| `zero_total` | a bruttó nulla (a negatív, sztornó végösszeg nem gyanús) |
| `net_exceeds_gross` | a nettó nagyobb a bruttónál |
| `totals_mismatch` | nettó + ÁFA ≠ bruttó |
| `vat_rate_mismatch` | az ÁFA nem egyezik a számlán szereplő ÁFA-kulccsal |
| `line_items_mismatch` | a tételsorok összege nem egyezik a végösszeggel |
| `due_before_issue` | a fizetési határidő a kiállítás előtt van |
| `performance_after_due` | a teljesítés a fizetési határidő után van |
| `date_out_of_range` | valószínűtlen dátum |
| `same_party` | a kiállító és a vevő azonos |
| `company_not_found` | a saját cég nem szerepel a számlán |
| `no_direction` | a saját cég nem ismerhető fel egyértelműen; kimarad az összesítésből |
| `currency_not_quoted` | az MNB nem jegyzi a devizát; nem váltható át, kimarad az összesítésből |

**Árfolyamhibák.** A számla tárolva marad, de amíg nincs árfolyama, kimarad az összesítésből; a számlalistában látszik. Újrafeltöltés nem kell: az árfolyamot a kimutatás minden lekérésekor újra keressük, így a `mnb_unavailable` az MNB visszatérése után magától megszűnik.

| Kód | Jelentés |
|---|---|
| `mnb_unavailable` | az MNB nem érhető el (legfeljebb óránként próbáljuk újra) |
| `mnb_rejected` | az MNB elutasította a lekérést |
| `mnb_no_rate` | `MNB_LOOKBACK_DAYS` napon belül nincs jegyzés a devizára |

**API- és indítási hibák.** Az API válasza `{"error": {"code": …, "message": …}}`.

| Kód | HTTP | Jelentés |
|---|---|---|
| `no_files` | 400 | a feltöltési kérésben nincs fájl |
| `invalid_ids` | 400 | érvénytelen azonosítólista |
| `processing_active` | 409 | törlés feldolgozás közben |
| `internal_error` | 500 | váratlan hiba; az alkalmazás tovább fut |
| `config_error` | – | hiányzó vagy érvénytelen kötelező beállítás (például üres `OPENROUTER_API_KEY`); az alkalmazás el sem indul |

## Adatbázis és migrációk

SQLite a volume-on (`/app/data/app.db`). Táblák: `invoices`, `exchange_rates`, `rate_coverage`, `mnb_currencies` (`app/db/tables.py`). A pénzösszegek szövegként tárolt `Decimal`-ok, így nincs lebegőpontos kerekítés. A sémát Alembic-revíziók követik (`app/db/migrations/versions/`). Induláskor az alkalmazás egy tranzakcióban a legfrissebb revízióra hozza az adatbázist: ha egy lépés elbukik, semmi sem marad félkészen.

Sémaváltoztatás: a `tables.py` módosítása után Dockerben generáljuk a revíziót, és átnézzük:

```
docker run --rm -u root -v "${PWD}\app\db\migrations:/app/app/db/migrations" <kép> sh -c "mkdir -p data && alembic revision --autogenerate --rev-id 0002 -m 'rovid leiras'"
```

Az üzenet legyen ékezet nélküli, mert ebből lesz a fájlnév. A futó konténerben `docker compose exec app alembic current` mutatja a revíziót, és `alembic check` jelzi, ha a modell és a migrációk eltérnek; ezt egy teszt is ellenőrzi. Ha egy korábbi fejlesztői verzió volume-ja nem frissíthető, `docker compose down -v` után újra kell tölteni a számlákat.

## Tesztek

pytest. Hálózatot nem használnak: az MNB és az OpenRouter `httpx.MockTransport`-tal, rögzített válaszokkal fut. Valódi LLM-et a tesztek nem hívnak, mert a kapott kulcs csak az alkalmazásba épített hívásokra használható.

| Terület | Fájlok |
|---|---|
| adatkinyerés | `extraction/test_normalize.py`, `test_validation.py`, `test_line_items.py`; `services/test_invoice_service.py` (kötelező mezők, irány, partner, duplikáció); `services/test_pdf_service.py`; `clients/test_llm_client.py` (kliens, hibakódok, e2e feltöltés mockolt LLM-mel) |
| árfolyam-kiválasztás | `services/test_rate_service.py` (megelőző nap, lookback, `unit`, hiányzó időszakok, single-flight, kiesés), `clients/test_mnb_client.py` |
| aggregálás | `services/test_report_service.py` (havi bontás, üres hónapok, partnerösszesítő, kerekítés, sztornó, jövőbeli becsült árfolyam, ellenőrzendő kihagyása, csoportok) |
| egyéb | feltöltés, sor, API, konfiguráció, i18n, migráció |

Futtatás Dockerben (a kép a fejlesztői függőségek nélkül készül, ezért ezeket a konténerben telepítjük):

```
docker build -t szamla-app .
docker run --rm -u root -v "${PWD}\tests:/app/tests:ro" -v "${PWD}\pytest.ini:/app/pytest.ini:ro" -v "${PWD}\requirements-dev.txt:/app/requirements-dev.txt:ro" szamla-app sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
```

## Ismert korlátok

- **LLM nélkül nincs kinyerés:** nincs szabályalapú tartalék. Kulcs nélkül az alkalmazás el sem indul; ha a kulcs érvénytelen, a keret elfogyott vagy az LLM nem elérhető, minden fájl LLM-hibakódot kap (`llm_auth`, `llm_quota`, `llm_unavailable` stb., lásd „Hibakódok”), és újrafeltöltéssel próbálható újra.
- **Nincs LLM-keret:** minden új fájl egy hívás; a már feldolgozott fájlt nem küldjük újra. -> Bemenet méretkorlátja: túl hosszú szövegnél hiba vagy csonkolás. Ez a hívásonkénti költséget fogja meg, de a többoldalas számláknál kockázatos.
- **Nem felvett hónapnév:** ha a dátum nem felvett nyelvű hónapnévvel szerepel (például „15 maart 2026”), a grounding csak a napot és az évet ellenőrzi, a hónapot nem.
- **Partnerazonosítás:** csak a magyar adószám-formákat egységesítjük. Külföldi partnernél, ha az egyik számlán EU-adószám, a másikon nemzeti adószám szerepel, ugyanaz a cég két oszlopba kerülhet.
- **Irány nélküli számla:** ha a saját cég szokatlan alakban szerepel (más adószámforma, eltérő jogi forma), a számla `no_direction` jelölést kap, és kimarad az összesítésből; a hiány csak a számlalistában látszik.
- **Előlegszámla:** az előre fizetett előlegszámla (`due_before_issue`) ellenőrzendő jelölést kap, és kimarad a kimutatásból. A validáció nem ismeri a számlatípusokat; a sztornót csak a negatív végösszeg jelzi.
- **Jövőbeli teljesítés:** a devizás számla a mai árfolyamon, becsülve kerül a kimutatásba, így a forintösszeg a teljesítés napjáig változhat. MNB-kiesés alatt a jövőbeli devizás számla `mnb_unavailable` jelölést kap, mert a mai árfolyamot nem tekintjük véglegesen tároltnak.
- **MNB-kiesés:** a nem tárolt árfolyamú devizás számlák kimaradnak az összesítésből; a diagram és az összesítő alatt a darabszámuk látszik, az ok a számlalistában.
- **Rögzített kimutatás-tartalom:** nem állítható, mely csoportok (sztornó, jövőbeli, ellenőrzendő) kerüljenek a diagramba és az összesítőbe.
- **Tételsor-ellenőrzés:** csak felismerhető mennyiség × egységár = sorösszeg szerkezetnél fut, legalább 2 sorral.
- **A kinyert adat nem javítható:** nincs szerkesztés és jóváhagyás, csak az összes számla törlése és újrafeltöltés. A PDF-et szándékosan nem tároljuk, ezért újrakinyeréshez újra fel kell tölteni.
- **Memóriabeli állapot, egy worker:** a folyamatban lévő és a hibás fájlok listája újraindításkor elvész (a hibanapló megmarad), és minden felhasználónak közös. Új feltöltés indulásakor a lista kiürül, egy párhuzamos menet hibáival együtt. Emiatt az alkalmazás szándékosan egy uvicorn-folyamattal fut.
- **Nincs hitelesítés:** bárki, aki eléri az oldalt, törölheti az összes számlát.
- **Skálázás:** a számlalista lapozás nélkül töltődik be, a kimutatás minden lekérdezéskor újraszámolódik.
- **A felület csak magyar,** de a szövegek nyelvi fájlban vannak.

## Továbbfejlesztési ötletek

- Pontosságmérés referenciaértékekkel (golden set) éles LLM-mel, mezőnkénti hibaaránnyal, és modellek összevetése.
- Szerkesztés és jóváhagyás az ellenőrzendő számlákra, egyenkénti törlés.
- Számlatípus (sztornó, előleg, helyesbítő) felismerése LLM-mezővel és típusfüggő szabályok, hogy az előlegszámla ne legyen ellenőrzendő.
- Állítható kimutatás-tartalom: a felületen ki-be kapcsolható, mely csoportok (sztornó, jövőbeli teljesítésű, ellenőrzendő) kerüljenek a diagramba és az összesítőbe; esetleg csoportonként külön kimutatás (például csak a jövőbeli számlák várható cash-flow-ja, vagy az ellenőrzendők összege).
- Rugalmasabb partner- és saját cég-egyezés: ország-előtag nélküli számjegymag, jogi forma nélküli név, a fél összes azonosítójának tárolása, kézi összevonás.
- A feldolgozási állapot az adatbázisba kerüljön (tartós, több worker, menetenkénti hibalista).
- Export (CSV vagy Excel, a diagram képként), nyomtatóbarát nézet, lapozás, szűrés, rendezés.
- LLM-keret (napi hívásszám-korlát), egyszerű hitelesítés, a hibanapló letöltése a felületről, további felületi nyelvek.
- Statikus kódelemzés (például SonarCloud) és lefedettségmérés CI-ban.
