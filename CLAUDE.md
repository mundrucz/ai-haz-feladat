# Fejlesztési szabályzat (AI-asszisztensnek és fejlesztőnek)

A feladat teljes leírása: `FELADAT.md`. Ez a fájl a munkamódot rögzíti; minden AI-eszköz (Copilot, Claude Code stb.) ezt kapja kontextusként.

## Projekt

Számlafeldolgozó webalkalmazás: PDF számlák feltöltése, adatkinyerés, MNB-árfolyammal HUF-ra váltás, cash-flow kimutatás. Cégünk: Nordwind Solutions Kft. (adószám `26481937-2-43`, EU VAT `HU26481937`).

## Stack

- Python 3.14 (a legfrissebb, amire minden függőség települ: a PyMuPDF által behúzott `onnxruntime` 3.15-re még nem ad wheelt; a Docker-kép patch-verzióra és Debian-kiadásra rögzítve), FastAPI, Jinja2 sablonok, SQLite (Docker volume-on) SQLAlchemy 2 ORM-mel (async, `aiosqlite`), sémaváltozás Alembic-revízióval (induláskor automatikusan, egy tranzakcióban fut)
- PDF-szöveg: PyMuPDF
- MNB: nyers SOAP `httpx`-szel, `xml.etree`-vel; az árfolyamok SQLite-ban cache-elve
- LLM: OpenRouter (OpenAI-kompatibilis API), a szolgáltató URL, a modell és a kulcs környezeti változóból állítható (`OPENROUTER_MODEL` stb.); alapmodell `openai/gpt-4o-mini` (a fiók csak ZDR-végpontot enged)
- Diagram: Chart.js, a JS fájl a repóban (nem CDN)
- Tesztek: pytest
- Futtatás: `docker compose up --build`, a Dockeren kívül semmi lokális függőség

## Általánosság: nyelv és pénznem (alapelv)

- Az alkalmazás bármilyen nyelvű és pénznemű számlán működik: a kinyerést az LLM végzi. Az ellenőrzésekhez használt adatfájlok (hónapnevek, ÁFA-címkék, devizajelek) csak a felvett nyelveket és devizákat ismerik: jelenleg a `FELADAT.md` nyelveit és devizáit. Bővítés csak adatfájllal (`app/extraction/resources/languages/`, `resources/currencies.json`), kódot nem kell módosítani. Ami nincs felvéve, az kinyerhető marad, csak az ellenőrzés gyengébb.
- **Tilos** a kódba égetett nyelv- vagy pénznem-felsorolás (`if currency in ("EUR", "USD", ...)`, nyelvenként külön kódág, beégetett devizalista az MNB-lekérdezésben, a validációban vagy a teszt-elvárásokban). A felsorolás helye kizárólag az adatfájl.
- **Pénznem:** az ISO 4217 kódot kell kinyerni/normalizálni (kód, szimbólum, név alapján; az LLM bármely háromjegyű kódot adhat, a devizafájl jeleit és neveit az összevetéshez használjuk), a támogatott devizák körét az MNB `GetCurrencies` válasza adja, nem a kód. Az árfolyam-lekérdezés a számlákon ténylegesen előforduló devizákra dinamikusan megy; az `unit` egységet mindig figyelembe vesszük. Ha az MNB nem jegyez egy devizát, az a számla hibája/figyelmeztetése (érthető ok), nem az alkalmazásé.
- **Nyelv:** nincs nyelvfelismerés-függőség és nincs nyelvenkénti elágazás. A nyelvenkénti adatfájlokat az ellenőrzések egyszerre, nyelvtől függetlenül használják. Ismeretlen nyelvű számla attól még feldolgozható.
- **Számok:** a tizedes- és ezreselválasztót a szám szerkezetéből és a számlán belüli konzisztenciából határozzuk meg (`1 234,56`, `1.234,56`, `1,234.56`, `1'234.56` stb.), nem nyelvből.
- **Dátumok:** a kimenet mindig ISO 8601 (`ÉÉÉÉ-HH-NN`). A dátumot az LLM adja ISO-ban; ellenőrizzük, hogy a szövegben bármely alakban szerepel-e (grounding), valós-e, észszerű tartományba esik-e, és a sorrend (kiállítás ≤ határidő, teljesítés ≤ határidő) stimmel-e.
- **Saját cég felismerése** nyelvtől függetlenül: a kinyert fél adószáma vagy neve egyezik (formátum-normalizálva) a beállított adószámmal, EU VAT-tal vagy névvel.
- A tesztek is általánosak: legyenek köztük a nem felvett nyelvek és devizák (pl. JPY, CHF, SEK, CZK, RON, nem latin írásrendszer). Elvárás: a normalizálás, a grounding és a validáció elfogadja őket.

## Architektúra-elvek (bővíthetőség, skálázhatóság)

- **Önálló modulok:** minden modul (pdf, kinyerés, validáció, árfolyam, riport, tárolás) önmagában is használható és tesztelhető; nincs globális állapot, rejtett importfüggés vagy közvetlen környezeti változó olvasás a modulok mélyén.
- **Függőséginjektálás:** a függőségeket (HTTP-kliens, LLM-kliens, adatbázis/repository, óra, konfiguráció) kívülről kapják meg (konstruktor/paraméter, FastAPI `Depends`), a szolgáltatások interfész (`Protocol`) mögött élnek, így cserélhetők és mockolhatók. Az összeállítás egyetlen helyen (composition root) történik.
- **Általános kliensek:** pl. az MNB-kliens ne legyen a feladatra szabva: bármely deviza, bármely időszak, a cél-deviza ne legyen beégetve; a mi cégünk adatai konfigurációból jönnek, nem kódból.
- **Konfigurálható:** saját cég (név, adószám, EU VAT), kimutatás pénzneme (alap: HUF), nyelv/locale (alap: hu), LLM (szolgáltató URL, modell, kulcs, időtúllépés, párhuzamosság), webes port (`APP_PORT`, alap: 8080). Az adatbázis útvonala Dockerben rögzített (`/app/data/app.db`, a volume-on), a `.env` nem írhatja felül; a `Settings` mezőként csak a tesztek kapják meg. Mind környezeti változóból (`.env.example`). A `.env` kötelező (nélküle a `docker compose` nem indul). A saját cég adatainak (név, adószám, EU VAT) szándékosan nincs alapértéke, mert egy rossz alapérték csendben hibás eredményt adna; hiányukban az alkalmazás érthető hibával nem indul el. A technikai beállítások dokumentált alapértékkel rendelkeznek, és mezőszintű megkötéseik vannak (pozitív szám, URL, felsorolt érték); érvénytelen érték esetén az alapérték érvényes, és ezt indításkor figyelmeztetés naplózza.
- **Új cég, új pénznem:** a kimutatás pénzneme és az árfolyam-forrás cserélhető legyen (pl. más cég EUR-ban kér kimutatást); a „HUF" nem lehet szétszórt konstans.
- **Bővíthető felület:** a szövegek nyelvi fájlokban (i18n), a szám- és dátumformátum a `DEFAULT_LOCALE`-hoz igazodik.
- **Async mindenhol, ahol lehet:** minden I/O-hívás aszinkron: HTTP (LLM, MNB), adatbázis (SQLAlchemy async session), fájlműveletek, webes végpontok (`async def`); az `async` az alapértelmezett, nem csak a PDF-feldolgozásnál. Ahol az egymástól független hívások párhuzamosíthatók, `asyncio.gather`-rel/`TaskGroup`-pal futtatjuk őket, korlátozott párhuzamossággal (szemafor), hogy ne terheljük túl az LLM-et, az MNB-t vagy az adatbázist. A kizárólag szinkron, CPU-igényes munka (PDF-szöveg kinyerés) szálkészletben fusson (`asyncio.to_thread`), hogy ne blokkolja az eseményhurkot. A feltöltés ne várja meg a teljes feldolgozást (háttérfeladat, állapotlekérdezés). Szinkron blokkoló hívás az eseményhurokban tilos.
- Ne építsünk előre „későbbi" funkciókat, de a kialakítás ne zárja ki őket.

## Munkamód

- **Függőleges szeletek:** minden változás backend + UI + teszt együtt, a végén futó, kipróbálható állapottal. Nem készítünk félkész réteget, amit nem lehet kipróbálni.
- Kódot, tesztfuttatást és commitot csak a felhasználó kérésére végzünk.
- Egy lépés = néhány kis, beszédes commit. Egyetlen „final" commit nem elfogadható. Ha sok változás gyűlt össze, témánként bontjuk commitokra (a tisztán mechanikus átrendezés, pl. fájlmozgatás, külön commitba kerül a tartalmi változásoktól), és minden commit önmagában is zöld tesztekkel fut.
- Minden szöveges fájl UTF-8 (BOM nélkül). Ékezetes tartalmú szkriptet nem csövezünk PowerShellből a `python -`-ba, mert a karakterek `?`-re cserélődnek; a szkriptet UTF-8 fájlba írjuk, és úgy futtatjuk.
- Commitot csak a felhasználó kérésére készítsünk. Az üzenet formátuma mindig `fejlesztes: {üzenet}`, rövid és tárgyszerű: mi történt, indoklás nélkül; a végén `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` sor.
- A munka a `fejlesztes` branchen megy (a `main`-en csak az üres README-s `init` commit van). Csak lokálisan commitolunk, **soha nem pusholunk** (a felhasználó kifejezett kérése nélkül).
- Nagyobb döntés vagy bizonytalanság esetén kérdezz, ne találgass.
- Minden fontosabb beszélgetésről kerüljön bejegyzés az `AI_NAPLO.md`-be időrendben: mit javasolt az AI, mit fogadtunk el/vetettünk el és miért, hol tévedett és hogyan derült ki.

## Titkok

- Az `OPENROUTER_API_KEY` **soha nem kerülhet a repositoryba**: csak `.env`-ből (gitignore-ban), a `.env.example` kulcs nélkül tartalmazza.
- A `FELADAT.md` kulcsot tartalmaz, ezért nem kerülhet commitba kulccsal (gitignore vagy a kulcs kivétele).
- A megadott kulcs csak az alkalmazásba épített LLM-hívásokra használható, fejlesztéshez nem. Tesztek és fejlesztői kísérletek nem hívhatnak valódi LLM-et.

## Adatkinyerés

- **Tilos a bedrótozás:** nincs fájlnévre, konkrét kiállítóra vagy a 100 db mintára szabott szabály. A rejtett tesztszámlák hasonlóak, de mások.
- **A feltöltött PDF-et nem tároljuk**, csak a kinyert adatokat (és a duplikáció-szűréshez a tartalom-hasht). A PDF csak a feldolgozás idejéig él memóriában. Ha nem sikerült minden kötelező mezőt kinyerni (számlaszám, felek neve és adószáma, kiállítás, teljesítés és fizetési határidő, pénznem, bruttó) vagy az adat érvénytelen, a feldolgozás hiba: a számla nem kerül tárolásba, a hiba (fájlnév, ok, érintett mezők) a feldolgozási listában látszik. A teljesítés dátumára nincs tartalék (az MNB-árfolyam napja). A nettó és az ÁFA opcionális: a hiányuk nem hiba.
- A kinyerés eredménye mindig séma-validáción megy át (típusok, kötelező mezők, dátum- és pénznemformátum).
- Ellenőrzések: a tételsorok összege egyezzen a végösszeggel (ahol van tételsor), a bruttó legyen nagyobb vagy egyenlő a nettónál, a számlán szerepeljen a mi cégünk adószáma vagy neve (különben gyanús).
- A „bruttó végösszeg" a fizetendő összeg: nem nettó, nem ÁFA. A fizetési határidő, a kiállítás és a teljesítés dátuma három külön mező.
- **LLM-kinyerés:** a mezőket az LLM nyeri ki, fájlonként egy hívással. Ugyanazt a fájlt (hash) nem küldjük el újra; az eredményt tároljuk. Az LLM válaszát nem fogadjuk el vakon: minden értéknek szerepelnie kell a dokumentumban (grounding), utána jön a validáció. Az irányt és a partnert nem az LLM adja, hanem a kód dönti el.
- Az LLM nem kapcsolható ki. Állítható: szolgáltató URL (OpenAI-kompatibilis), modell, kulcs, időtúllépés, párhuzamosság környezeti változóból. Kulcs nélkül (hiányzó vagy üres `OPENROUTER_API_KEY`) az alkalmazás érthető hibával nem indul el.
- Hibás vagy olvashatatlan PDF nem állíthatja le az alkalmazást: a hiba az adott fájlnál jelenik meg.
- **Nincs duplikáció:** a PDF-et tartalom-hash-sel (SHA-256) azonosítjuk, és az adatbázis egyedi kulccsal is kikényszeríti (nem csak az alkalmazáslogika); a kiállító + számlaszám is egyedi. Újrafeltöltés idempotens, és nem hívja újra az LLM-et. Párhuzamos feltöltés mellett is igaz (versenyhelyzet esetén az egyedi kulcs ütközését kezeljük, ne hibával álljon le).
- Partnerazonosítás az adószám alapján (normalizálva); a név nem azonosít, csak megjelenítésre szolgál.

## Pénz és árfolyam

- Pénzösszegeknél **mindig `Decimal`**, soha `float`.
- Az árfolyam a **teljesítési dátum** napi MNB-árfolyama; ha nincs jegyzés, az utolsó megelőző nap. Az `unit` mezőt figyelembe kell venni.
- Kerekítés számlánként egész forintra, `ROUND_HALF_UP`; összesítés csak ezután.
- Egy napot/devizát nem kérdezünk le kétszer: a lekért árfolyamokat tároljuk (kulcs: deviza + nap, egyedi kulccsal). Lekérés előtt megnézzük, mi van már tárolva; csak a hiányzó időszakot kérjük le, a hiányzó devizákat/napokat egy kéréssel, tartományként. Párhuzamos kéréseknél ugyanaz a lekérés egyszer fusson (single-flight). A „nem volt jegyzés" napokat is megjegyezzük, hogy ne kérdezzük újra.
- Az MNB végpontja csak `http`-n működik.

## Hibakezelés

- Cél: **semmilyen bemenet vagy külső hiba nem állíthatja le az alkalmazást**; a hiba az érintett fájlnál/számlánál jelenik meg érthető okkal, a többi feldolgozás folytatódik.
- Feldolgozási állapotok fájlonként/számlánként: sorban/feldolgozás alatt, kész (`ok`), ellenőrzendő (`needs_review`, gyanús érték, tárolva), hibás (ok kóddal és üzenettel, nem tárolva). A hibás fájl újrafeltöltéssel próbálható újra.
- Számba veendő hibaokok: nem PDF vagy sérült/jelszavas/üres/szkennelt PDF; túl nagy fájl; nem számla; hiányzó vagy ellentmondó mező; ismeretlen nyelv/formátum; dátum- és számformátum-hiba; ismeretlen pénznem; LLM nem elérhető, időtúllépés, rate limit, kvóta, érvénytelen kulcs, nem JSON vagy sémának nem megfelelő válasz; MNB nem elérhető/időtúllépés/hibás XML, hiányzó árfolyam; adatbázis-zárolás vagy ütközés; párhuzamosság okozta versenyhelyzet; lemez/hely hiánya.
- Külső hívásoknál időtúllépés, korlátozott újrapróbálás exponenciális visszavárással (átmeneti hibákra), és érthető hibatípus; végleges hibát nem próbálunk újra.
- Saját kivételhierarchia (domain-hibák), a határokon (API, háttérfeladat) elkapva és naplózva; csupasz `except:` és elnyelt hiba tilos. Strukturált naplózás (fájl/számla azonosítóval), titkok nélkül.
- A hibaágakra is írunk tesztet (rossz PDF, LLM-hiba, MNB-hiba, ütköző feltöltés).

## Tesztelés

- Minden új működéshez pytest teszt tartozik; kötelező lefedni: adatkinyerés, árfolyam-kiválasztás, aggregálás.
- A tesztek nem használnak hálózatot: az MNB és az OpenRouter HTTP-szinten mockolt (`httpx.MockTransport`), rögzített válaszokkal fut. Valódi LLM-et teszt nem hív.
- Hibás eredmény esetén az okot az általános logikában keressük; nem „igazítjuk" a kódot az adott számlához.
- Egy lépés csak akkor kész, ha a tesztek zöldek, és a működést ténylegesen kipróbáltuk (Dockerben is, ahol releváns).

## Kód és struktúra

- Nyelv: kód és azonosítók angolul; UI, README, napló, commit magyarul.
- Kicsi, egy felelősségű modulok, rétegek szerinti mappákban: `core` (config, hibák, i18n, naplózás), `db`, `repositories` (csak tárolás), `clients` (külső HTTP), `services` (üzleti logika), `extraction`, `api` (webréteg, üzleti logika nélkül), `web` (sablonok, statikus és nyelvi fájlok). A tesztek ugyanezt a mappaszerkezetet követik. A fájlnév utótagja jelzi a réteget (`*_repository.py`, `*_service.py`, `*_client.py`, `*_routes.py`, `*_config.py`), a teszt neve `test_<modul>.py`. Új modul a rétegének megfelelő mappába és névvel kerül.
- Típusannotációk; kommentet csak ott írunk, ahol a kód magától nem érthető.
- Nincs felesleges függőség vagy előre megírt „későbbi" funkció.
- A UI: egy oldalon feltöltés, diagram, cégenkénti táblázat, számlalista; a szám formátuma olvasható (ezres tagolás), a hibák fájlonként láthatók. A UI szövegei nyelvi fájlból jönnek (alapértelmezett: magyar), nincs beégetett szöveg a sablonokban és a kódban.

## Leadandó és értékelés (FELADAT.md alapján)

- Egy paranccsal induljon (`docker compose up --build`); az elérési URL (pl. `http://localhost:8080`) a README-ben. A `.env.example`-t `.env` néven másolva és a kulcsot beírva induljon el; a modell neve `OPENROUTER_MODEL`-ből konfigurálható.
- Feltöltés: egyszerre több PDF (a teljes 100 db is), eredmény frissítés után is megmarad, fájlonkénti hibajelzés.
- Kinyerendő mezők: számlaszám, kiállító és vevő (név, adószám), kiállítás/teljesítés/fizetési határidő dátuma, pénznem, bruttó végösszeg, irány (`bejövő`/`kimenő`), partner.
- Kimutatás egy oldalon, ebben a sorrendben, minden összeg bruttó HUF: (a) havi vonaldiagram fizetési határidő szerint (bejövő és kimenő vonal, üres hónapok 0-val, jelmagyarázat, tengelyfeliratok, ezres tagolás), (b) cégenkénti összesítő táblázat (oszlop: partner; sor: Össz bejövő, Össz kimenő; hiányzó irány `0`), (c) számlalista az eredeti összeggel/pénznemmel, felhasznált MNB-árfolyammal és dátumával, HUF-összeggel.
- A fájlnevekre nem építünk. A kiértékelés rejtett, hasonló számlákkal is történik; a feladat példái (nyelvek: hu, en, de, fr, es, it, pl, kétnyelvű; pénznemek: HUF, EUR, USD, GBP, PLN; formátumok: `1 234,56`, `1.234,56`, `1,234.56`, `2026.03.15.`, `15 mars 2026`, `March 15, 2026`) a feladat példái, de az alkalmazásnak **nem korlátai**, lásd az „Általánosság" szakaszt. Többoldalas számlák is lesznek.
- Kötelező dokumentáció: `README.md`, `AI_NAPLO.md` (időrendben: lépések, meghatározó promptok, elfogadott/elvetett javaslatok, AI tévedései és javításuk, kontextusfájlok); ha az eszköz engedi, beszélgetés-export az `ai-logs/` mappában. A szabályfájlok (ez a fájl is) a repóban maradnak.
- Kötelező automatikus tesztek: legalább adatkinyerés, árfolyam-kiválasztás, aggregálás.
- Bemutatóra készülés (~45 perc): fejlesztés menete, döntések és alternatívák (szabályalapú vs. LLM, árfolyam-kezelés, adatmodell), hol nem bíztunk az AI-ban, élő Docker-demó (100 számla, kimutatás, egy hibás/szokatlan számla), és egy kisebb módosítás élőben AI-val. A kódot ezért úgy írjuk, hogy a fejlesztő magyarázni és gyorsan módosítani tudja.
- Értékelési szempontok: helyesség, robusztusság, AI-asszisztált fejlesztés tudatossága, bemutató, kódminőség, Docker-működés, felhasználói élmény, dokumentáció (a korlátok őszinte leírása).

## Dokumentáció

- A `README.md` tartalma: indítás és URL, konfiguráció, architektúra és service-ek, a feldolgozás menete, adatkinyerés és ismeretlen formátumok kezelése, LLM-modell és indoklás, az LLM-válasz ellenőrzése, irány/partner/duplikáció, MNB lekérés és tárolás, kimutatás, hibakezelés és naplózás, adatbázis és migrációk, tesztek, ismert korlátok, továbbfejlesztés.
- Ha egy változtatás a viselkedést érinti, a kapcsolódó dokumentációt is frissítjük.

