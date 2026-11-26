# AI napló

Az AI-asszisztált fejlesztés időrendi naplója. Minden bejegyzésben külön látszik:
- **Kérés:** amit a fejlesztő kért vagy eldöntött
- **AI javaslata:** amit az AI ajánlott, és hogy elfogadtuk vagy elvetettük;
- **Eredmény:** ami ténylegesen elkészült;
- **AI tévedése:** hol hibázott az AI, és hogyan derült ki.

Eszköz: GitHub Copilot CLI. Kontextusfájlok:
- `CLAUDE.md`: fejlesztési szabályzat, minden munkamenet megkapja;
- `FELADAT.md`: a feladat leírása; az API-kulcs miatt nincs a repóban.

---

## 2026-11-23

### Stack kiválasztása
- **Kérés:** „Milyen technológiai stacket javasolsz a feladathoz?”
- **AI javaslata (elfogadva):**
  - Python és FastAPI, Jinja2 sablonok, SQLite Docker volume-on, PyMuPDF.
  - Az MNB-hez nyers SOAP `httpx`-szel, külön SOAP-könyvtár nélkül.
  - Chart.js a repóban, pytest, Docker Compose.
  - Indoklás: a változatos nyelvek és elrendezések miatt LLM-es kinyerés kell, mellé determinisztikus ellenőrzés.

### Feladatbontás
- **Kérés:** „Bontsuk a feladatot lépésekre úgy, hogy a frontend és a backend minden lépésben együtt haladjon, és minden lépés kipróbálható legyen.”
- **AI javaslata (elfogadva):** függőleges szeletek. A lépések: váz, feltöltés, kinyerés, irány és partner, MNB, kimutatás, robusztusság, UX, zárás.
- **Kérdés:** mire való az „irány és partner” lépés?
  - **AI magyarázata:** itt dől el, hogy a számla bejövő vagy kimenő (a saját cég a kiállító vagy a vevő). Itt dől el az is, ki a partner (normalizált adószám alapján).
  - **Döntés:** a magyarázat után önálló lépés maradt.

### Fejlesztési szabályzat
- **Kérés:** „Első lépésként készíts fejlesztési szabályzatot.” Kiegészítések:
  - a modulok legyenek önállóak, a függőségeket kívülről kapják;
  - a rendszer legyen bővíthető (más cég, más kimutatási deviza, más nyelv);
  - az LLM legyen konfigurálható;
  - kapjon hangsúlyt a hibakezelés;
  - se a PDF-ek, se az MNB-lekérdezések ne duplikálódjanak.
- **Eredmény:** `CLAUDE.md`. Szakaszai: architektúra-elvek, hibakezelés, titokkezelés, pénzkezelés (`Decimal`, `ROUND_HALF_UP`), MNB-szabályok, tesztelés.
- **AI tévedése:** az aszinkron működést csak a PDF-feldolgozásra írta elő. A fejlesztő pontosított: minden I/O legyen aszinkron, ahol csak lehet. A szabály ennek megfelelően módosult.

### Általánosság mint alapelv
- **Kérés:** „A FELADAT.md-ben felsorolt nyelveket és pénznemeket kezeld úgy, mintha nem léteznének: a megoldás bármilyen nyelvű és pénznemű számlán működjön.”
- **Eredmény:** új szakasz a `CLAUDE.md`-ben:
  - nyelv- vagy devizalistára épülő logika tilos;
  - a pénznem ISO 4217 kódra normalizálódik, a támogatott devizákat az MNB adja;
  - a tesztekben szokatlan nyelvek és devizák is szerepelnek.

### Váz és feltöltés
- **Kérés:** „Kezdjük a fejlesztést a váz felállításával, utána jöjjön a feltöltés részletes terve.”
- **Eredmény:**
  - FastAPI composition root, i18n, saját kivételhierarchia, Docker (nem root felhasználó, healthcheck).
  - Többfájlos feltöltés, tartalom-hash alapú duplikáció-szűrés.
  - Háttérfeldolgozás; a PDF-szöveg kinyerése szálban fut (`to_thread`).
- **Kérés:** „Szükség van-e arra, hogy egy PDF újra feldolgozható legyen?”
  - **AI javaslata (elfogadva):** külön újrapróbálás-gomb nincs. A hibás fájl újrafeltölthető.
- **Kérés:** ha a feltöltés még folyamatban van, az oldal elhagyása előtt figyelmeztessen.
  - **AI pontosítása:** csak a még el nem küldött fájlok vesznek el, a figyelmeztetés ezekre vonatkozik.
- **AI tévedése:** egy kötegben többször szereplő ugyanazon fájl párhuzamos mentése Windows alatt `PermissionError`-t okozott. A teszt hozta ki, javítva.

### A PDF-ek nem tárolódnak
- **Kérés:** „A feltöltött PDF-eket ne tárold, csak a kinyert adatokat. Ha valamelyik adatot nem sikerült kinyerni, az hiba, és semmi ne tárolódjon.”
- **Eredmény:** a PDF csak a feldolgozás idejéig él a memóriában. A hiányos számla hibás, az ok a felületen látszik.
- **AI tévedése:** a pozíció szerint rendezett szövegkinyerés összefésülte az oszlopos számlák sorait. A teljes út (feltöltés, feldolgozás, lista) futtatásakor derült ki, ezért átálltunk a tartalomfolyam sorrendjére.

### Kinyerés LLM-mel
- **Kérés:** „Ha egy PDF feldolgozása nem sikerül, a felület jelezze, melyik fájlról van szó és mi a hiba oka.”
- **Eredmény:**
  - OpenAI-kompatibilis LLM-kliens; a párhuzamosság, az időkorlát és az újrapróbálás konfigurálható.
  - Külön hibakódok: elérhetetlen szolgáltatás, rate limit, kulcs, kvóta, érvénytelen válasz.
- **Kérés:** „Készítsd elő úgy, hogy az LLM-mel ki tudjam próbálni.”
  - **Kiderült:** a fiók csak zero-data-retention végpontokat enged, ezért 404-et kaptunk.
  - **Eredmény:** a modell `openai/gpt-4o-mini` lett, és a 404 külön hibakódot kapott.

### Az LLM-válasz ellenőrzése
- **Kérés:** „Vessük össze az LLM által visszaadott mezőket a PDF szövegével.”
  - **Eredmény:** grounding. Minden visszaadott értéknek szerepelnie kell a számla szövegében, különben a válasz érvénytelen (hallucináció ellen).
- **Kérés:** „Ellenőrizzük azt is, hogy az ÁFA-kulcs összhangban van-e a nettó összeg és az ÁFA arányával.”
  - **Eredmény:** eltérés esetén `vat_rate_mismatch`, és a számla „ellenőrzendő” lesz.
- **Kérés:** „Hogyan szűrjük a duplikátumot, ha a PDF már fel volt dolgozva?”
  - **AI javaslata (elfogadva):** a fájl-hash mellett a kiállító adószáma és a számlaszám is azonosít. Így az eltérő bájtú, de azonos számla is kiszűrhető.

### MNB-árfolyam és kimutatás
- **Kérés:** „Készítsd el az MNB-árfolyam lekérését és a kimutatást.”
- **Eredmény:**
  - SOAP-kliens; az árfolyamok tárolódnak, a jegyzés nélküli napok is. Csak a hiányzó időszakot kérjük le, single-flight lekéréssel.
  - Az árfolyam a teljesítés napjára vagy az utolsó előző jegyzésre vonatkozik; a `unit` mezőt figyelembe vesszük.
  - Havi vonaldiagram, partnerösszesítő és számlalista.
- **AI javaslata (elfogadva):** az MNB-hiba csak az érintett számlát jelöli meg, a kimutatás többi része működik.

---

## 2026-11-24

### A feladatleírás újbóli átvizsgálása
- **Kérés:** „Olvasd el újra a FELADAT.md-t: mi az, ami nem vagy nem helyesen valósult meg?”
- **AI tévedése:** a partnerösszesítő táblázat fordítva készült el (a cégek a sorokban szerepeltek, nem az oszlopokban). A tesztek csak az API adatait nézték, ezért nem fogták meg. A fejlesztő kérésére javítva.

### A felület finomítása
A fejlesztő kérései, egyenként megvalósítva:
- a feltöltési dobozban csak összesítő jelenjen meg (sikeres, hibás, duplikátum), fájllista nélkül;
- a „Kinyert számlák” blokkban 10 sor látsszon, a többi görgethető legyen;
- a „Feldolgozás állapota” blokk neve legyen „Hibás fájlok”; mutassa a hiba okát és a feltöltés idejét, méret oszlop nélkül;
- a „Fájlok kiválasztása” gomb legyen szürke, amíg le van tiltva;
- legyen „Összes számla törlése” gomb.
  - **AI javaslata (elfogadva):** a gomb a „Kinyert számlák” fejlécébe került, megerősítő párbeszéddel. Futó feldolgozás alatt nem engedélyezett.
- a diagram ne mutasson buborékot (tooltip) egérrámutatásra.

### Feltöltés leállítása és a hibás fájlok
- **Kérés:** „Mi történjen, ha egy folyamatban lévő feltöltést le akarunk állítani?”
  - **AI javaslata (elfogadva):** a böngésző nem küld több köteget, a szerver pedig törli a sorban állókat. A már futó fájlok befejeződnek.
- **Kérés:** a hibás fájlok listája ürüljön ki, amikor új feltöltés indul.
- **Kérés:** a hibás fájlokról készüljön napló.
  - **AI javaslata (elfogadva):** egyetlen, forgatott JSON Lines fájl (nem fájlonként külön), minden hibaforrással.
- **Kérés:** a lista csak a sikerteleneket mutassa, legfeljebb 500 sort.
- **Kérés:** „Hogyan ellenőrizzük a duplikátumot, és elég-e, ha az átnevezett PDF-et felismerjük?”
  - **AI válasza:** a két szint marad. Az LLM előtt a fájl-hash alapján (ugyanaz a fájl nem megy el még egyszer az LLM-nek). Az LLM után a kiállító és a számlaszám alapján, mert ezek csak a kinyerés után ismertek.

### README: korlátok és ötletek
- **Kérés (a fejlesztő ötletei):** a továbbfejlesztési ötletek közé kerüljön be a kiválasztható törlés, valamint az adatok, a diagram és a számlalista exportja, egyszerű nyomtatással.
- **AI javaslata (elfogadva):**
  - További ötletek: kézi szerkesztés és jóváhagyás, lapozás, LLM-költségkeret, tartós állapot, a napló letöltése, több nyelv.
  - A fejlesztő a megbeszélés után kérte, hogy ezek is kerüljenek a README-be.

---

## 2026-11-25

### Kötelező mezők
- **Kérés:** „A nettó összeg és az ÁFA legyen opcionális.” Utána: „A teljesítés dátuma mégis maradjon kötelező, és ne pótoljuk a kiállítás dátumával.”
- **Eredmény:** kötelező a számlaszám, a felek neve és adószáma, a három dátum, a pénznem és a bruttó összeg.
- **Kérés:** a felek adószáma a validációban is kötelező legyen.

### Ellenőrzések bővítése
- **Kérés:** a tételsorok összegét is vessük össze a végösszeggel, ahogy a feladat kéri.
  - **Eredmény:** eltérés esetén a számla „ellenőrzendő”.
  - **AI tévedése:** az első változat túl engedékeny volt, egy végösszeg is „megmagyarázhatta” az eltérést. Szűkítve.
- **AI javaslata (elfogadva):** az adatbázis is kikényszeríti a kiállító és a számlaszám egyediségét, ne csak az alkalmazáslogika.
- **AI javaslata (elfogadva):** a támogatott devizákat az MNB `GetCurrencies` válasza adja (tárolva). A nem jegyzett deviza a számla figyelmeztetése.
- **AI javaslata (elfogadva):** a hiányzó devizák egyetlen kérésben, legfeljebb egyéves darabokban mennek.
- **AI felvetése:** ha a saját cég nem ismerhető fel, a számla csendben kimarad az összesítésből.
  - **Döntés:** a négy lehetőség közül csak az „ellenőrzendő” jelölés készült el.
- **AI felvetése:** a távoli jövőbeli teljesítési dátumhoz nincs árfolyam.
  - **Döntés:** külön hibakód készült. Az előzetes árfolyam elvetve, ismert korlátként szerepel.
- **AI felvetése:** a külföldi partner eltérő adószám-formái két partnerként jelennek meg.
  - **Döntés:** README ismert korlátai között szerepel.

### Python-verzió
- **Kérés:** „Miért `python:3.12-slim`? Használjuk a lehető legfrissebb Python-verziót.”
- **Eredmény:** Python 3.14. A 3.15-re a PyMuPDF egyik függősége (`onnxruntime`) még nem ad wheelt. A kép patch-verzióra és Debian-kiadásra rögzítve, a reprodukálható build kedvéért.
- **Elvetve:** a régebbi PyMuPDF a 3.15 kedvéért.

### Konfiguráció
- **Kérés:** „Az alkalmazás ne induljon el `.env` fájl nélkül.” Pontosítások:
  - csak a cégadatok legyenek kötelezők;
  - a port az `.env`-ből jöjjön (alapértéke 8080);
  - érvénytelen technikai beállításnál álljon vissza az alapértékre, és naplózza.
- **AI javaslata (elfogadva):** csak az üzleti adatok kötelezők, mert egy rossz cégadat-alapérték csendben minden számla irányát elrontaná.
- **Kérés:** az adatbázis útvonala legyen konfigurálható.
  - **AI javaslata (elfogadva):** visszavonva, mert Dockerben a volume-on kívül adatvesztést okozna.
- További kérések:
  - ne legyen automatikus újraindítás;
  - a `REPORT_CURRENCY` és a `DEFAULT_LOCALE` kapjon alapértéket;
  - a naplózás szintje és formátuma ne legyen állítható;
  - a hibás fájlok naplója rögzített helyen legyen;
  - a felesleges `overrides` paraméter kerüljön ki;
  - az ismeretlen `DEFAULT_LOCALE` álljon vissza a magyarra.
- **AI tévedései:**
  - a Pydantic a mintát a nagybetűsítés előtt ellenőrizte (a teszt hozta ki);
  - téves állítás a `*_CONCURRENCY=0` hatásáról;
  - az `overrides` paraméter használatáról is tévesen nyilatkozott.

### Mappaszerkezet
- **Kérés:** „Rendezd a fájlokat mappákba (például szolgáltatások, repositoryk, konfiguráció), és a fájlnévből is derüljön ki a réteg.”
- **AI javaslata:** rétegek vagy funkciók szerinti szerkezet. A fejlesztő a rétegek szerintit választotta, a tesztekre is.
- **Eredmény:** `core`, `db`, `repositories`, `clients`, `services`, `extraction`, `api`, `web`; a fájlnevek utótagot kapnak (`*_service.py`, `*_repository.py` stb.).
- **AI tévedései:**
  - egy rétegsértés (a `core` a `services`-ből importált);
  - a PowerShellből csövezett szkript elrontotta az ékezeteket.
  - Mindkettő javítva. A `CLAUDE.md`-be bekerült, hogy ékezetes szkriptet UTF-8 fájlból futtatunk.

---

## 2026-11-26

### Adatréteg
- **Kérés:** „Muszáj nyers SQL-t használni?”, majd: „Írd át ORM-re.”
- **AI javaslata (elfogadva):** SQLAlchemy 2 ORM (async) és Alembic.
  - **Elvetve:** a SQLModel, mert saját dataclassaink vannak, és az upserthez úgyis SQLAlchemy kell.
  - **Elvetve:** a JSON-fájl, mert nincs benne egyedi kulcs.
- **Kérés:** tiszta lap, egyetlen kezdő migrációval.
- **AI tévedése:** a generált migráció alkalmazáskódra hivatkozott. Javítva: az alap SQL-típust írja ki.

### Újrapróbálás és terhelés
- **Kérés:** „HTTP-státusz alapján ellenőrizd mindkét kliensnél, van-e értelme újrapróbálni. Ha nincs, ne próbálkozzunk.”
  - **Eredmény:** csak átmeneti hibánál próbálunk újra (408, 425, 429, 5xx, időtúllépés).
  - **AI tévedése:** a magyarázatban egy sehol nem használt konstanst nevezett meg a döntés helyeként.
- **Kérés:** „Mi történik, ha a feltöltés jóval gyorsabb a feldolgozásnál?”
  - **AI javaslata (elfogadva):** korlátos feldolgozási sor. Tele sornál a feltöltés vár, így a memória nem telik meg.
  - **Elvetve:** a fájlok számának korlátozása a böngészőben, mert a szervert nem védi.
- **Kérés:** a leállított fájlok semmilyen listába ne kerüljenek be, csak a böngésző összegzése maradjon róluk.
- **Kérés:** „Mi történik, ha egy feltöltés közben valaki újabb feltöltést indít?”
  - **AI felvetése:** az új feltöltés a másik menet hibalistáját is kiüríti.
  - **Döntés:** a fejlesztő kérésére ismert korlátként került a README-be.
- **Kérés:** „Mi történik, ha az MNB egy napig nem érhető el?”
  - **AI felvetése:** egy elbukott lekérés az adott deviza minden számláját hibásnak jelölte, a már tárolt árfolyamúakét is.
  - **Döntés:** javítva. Az elérhetetlen MNB-t óránként próbáljuk újra.

### Egyszerűsítés
- **Kérés:** „Mely ellenőrzéseket végezzük kétszer? Töröld a duplikáltakat, kivéve azokat, amelyek máshonnan is hívódhatnak.”
  - **Eredmény:** a kétszeres kötelezőmező-ellenőrzés törölve. A pénznem-ellenőrzés marad, mert a kinyerő cserélhető.
- **Kérés:** „Miért kell a név tartaléknak? Az adószám kötelező.”
  - **Eredmény:** a partnerkulcs csak az adószám.
- **Kérés:** a CSS-ismétlések és az MNB-elutasítás téves hibaszövege legyen javítva.
- **Kérés:** „Az LLM ne legyen kikapcsolható, és csak azt teszteld, ami valódi LLM nélkül is tesztelhető.”
  - **Eredmény:** a kapcsoló és a beégetett LLM-válaszra épülő tesztek törölve. Az LLM-kliens tesztjei mockolt HTTP-vel maradtak.

### Dokumentáció
- **Kérés:** „Frissítsd a README-t és a CLAUDE.md-t.” Szempontok:
  - a README térjen ki mindenre, amit a FELADAT.md kér;
  - írja le, mi miért van és hogyan működik a folyamat;
  - legyen részletes, de tömör;
  - az elavult szabályok kerüljenek ki.
- **AI tévedése:** a korábbi README szerint mindkét kliens időkorlátja 20 mp volt, valójában az LLM-é 60 mp. Javítva.

### Ismert korlátok: kötelező LLM-kulcs
- **Kérés:** „Dolgozzunk az ismert korlátokon! LLM nélkül nincs kinyerés – maradjon az ismert korlátoknál, de ha üres az LLM-hez tartozó kulcs, ne induljon el az alkalmazás.”
- **AI javaslata (elfogadva):** az `OPENROUTER_API_KEY` ugyanúgy kötelező beállítás legyen, mint a cégadatok. Hiányzó, üres vagy csak szóközből álló kulcsnál a `load_settings` `ConfigError`-t dob, a konténer érthető hibával leáll.
  - Indoklás: kulcs nélkül egyetlen számla sem dolgozható fel; a korábbi „elindul, de minden fájl `llm_not_configured` hibát kap” működés csak a feltöltés után derítette ki a hibát.
- **Eredmény:**
  - a `llm_not_configured` hibakód, a hozzá tartozó szöveg, az `llm_available` jelző és a kulcs nélküli kódág törölve; az `InvoiceAnalyzer` mindig kap kinyerőt;
  - a tesztalkalmazás mockolt HTTP-klienst kap, amely minden külső hívást 401-gyel utasít el, így a szöveges PDF-ek `llm_auth` hibát kapnak, és a tesztek továbbra sem hívnak valódi LLM-et;
  - új tesztek: üres, szóközös és `.env`-ben üresen hagyott kulcs leállítja az indulást, a kulcs körüli szóköz levágódik;
  - kipróbálva Dockerben: üres kulccsal a konténer `OPENROUTER_API_KEY (Value error, must not be empty)` hibával kilép, kitalált kulccsal elindul;
  - a README, a `.env.example` és a `CLAUDE.md` frissítve; az ismert korlát megmaradt: nincs szabályalapú tartalék,   érvénytelen kulcsnál vagy LLM-kiesésnél minden fájl LLM-hibakódot kap.
- **AI tévedése:** a tesztkonstansok átnevezéséhez írt egysoros szkript a CRLF sorvégeket LF-re cserélte, így a diff az egész fájlt mutatta. A `git diff --stat` hozta ki; a sorvégek visszaállítva.
- **Kérés:** „Mit jelent az `llm_*` hiba?”, majd: „Ne az ismert korlátoknál, hanem legyen egy szekció a hibakódokról a README-ben.”
  - **Eredmény:** új „Hibakódok” szakasz a README-ben: fájlhibák, ellenőrzendő jelölések, árfolyamhibák, API- és indítási hibák, kódonként egy sorral. A korábbi szétszórt felsorolások (Hibakezelés, LLM-modell, Ismert korlátok) erre hivatkoznak.
  - **AI tévedése:** az első változat szerint minden árfolyamhiba magától megszűnik a következő frissítéskor. A kódból kiderült, hogy a jegyzés nélküli napokat megjegyezzük, ezért ez csak a `mnb_unavailable`-ra és a `mnb_rate_not_yet`-re igaz. Javítva.

### Sztornó, jövőbeli és ellenőrzendő számlák a kimutatásban
- **Kérés:** a sztornó számla kerüljön a kimutatásba. A jövőbeli számla is kerüljön be mai árfolyamon, és a felület jelezze, hogy ez nem pontos mert mai árfolyammal kalkuláltunk. A diagramnál és az összesítőnél látszódjon, mit tartalmaz a kimutatás és mit nem. A számlalista legyen csoportonként szűrhető (összes, sztornó, jövőbeli, ellenőrzendő); az ellenőrzendők ne kerüljenek a kimutatásba. A továbbfejlesztési ötletekbe kerüljön be az állítható kimutatás-tartalom.
- **Az AI kérdései és a döntések:**
  - az előlegszámla (`due_before_issue`) ellenőrzendő marad, így kimarad a kimutatásból (felhasználói döntés);
  - a „jövőbeli” a teljesítés dátumára vonatkozik, mert az árfolyam napja ez (felhasználói döntés);
  - a sztornót a negatív bruttó jelzi; számlatípus-mezőt nem vezettünk be (AI javaslata, elfogadva).
- **Eredmény:**
  - validáció: a `non_positive_total` helyett csak a nulla végösszeg gyanús (`zero_total`); a nettó–bruttó és az ÁFA-kulcs ellenőrzése negatív összegnél az ellentett értékekkel fut;
  - adatmigráció (`0002`): a korábban `non_positive_total`-lal tárolt sztornók elveszítik a jelölést, és ha más nem maradt, `ok` állapotba kerülnek;
  - árfolyam: a jövőbeli nap a mai napra szorul, így bármilyen távoli teljesítés a legutóbbi jegyzést kapja; a `mnb_rate_not_yet` hibakód megszűnt;
  - kimutatás: az ellenőrzendő számla átváltódik és látszik a listában, de kimarad a diagramból és az összesítőből. A `/api/report` új `coverage` (benne lévők, sztornók, becsültek, kimaradások okonként) és `groups` (csoportok darabszáma) mezőt ad;
  - felület: a diagram és az összesítő alatt lefedettségi sorok és figyelmeztetés a becsült árfolyamra; a számlalistában csoportszűrő gombok, „Sztornó” és „Becsült árfolyam” címke, megjegyzés az ellenőrzendő sorokon. A diagram görbéje monoton simítást kapott, mert a negatív sztornó mellett a korábbi simítás 0 alá lógott;
  - tesztek: sztornó-, nulla- és jövőbeli esetek, a migráció a régi jelölésű sorokon; kipróbálva Dockerben szintetikus számlákkal.
- **AI tévedése:** a korábbi `test_future_dates_are_not_requested` a régi szabályt rögzítette, és a célzott futtatáskor bukott el. Átírva az új szabályra (csak a mai napig kérünk).
- **AI tévedése:** a felület kipróbálásához beszúrt próbaszámla jelöléseit egyszerű szövegként írta az adatbázisba (`{code, field}` objektum helyett), így a lista üres maradt. A headless böngészős képernyőkép hozta ki; az adatot javította, az alkalmazás kódja jó volt.

### Időkorlátok
- **Kérés:** „20, 60 second, ezek nagyon magas timeoutok.”
- **AI javaslata:** különálló kapcsolódási és olvasási időkorlát; kapcsolódás 5 mp, LLM-olvasás 30 mp, MNB-olvasás 10 mp. Figyelmeztetett, hogy túl alacsony értéknél a lassú, de jó LLM-válasz is időtúllépést kap, és az újrapróbálás újabb fizetős hívás.
- **Döntés (felhasználó):** kapcsolódás 5 mp, LLM 15 mp, MNB 5 mp.
- **Eredmény:** új `HTTP_CONNECT_TIMEOUT_SECONDS` (5) beállítás, `LLM_TIMEOUT_SECONDS` 60 → 15, `MNB_TIMEOUT_SECONDS` 20 → 5. A kliensek `httpx.Timeout(olvasás, connect=kapcsolódás)` objektumot küldenek; tesztek ellenőrzik a kérésben átadott értékeket. A helyi `.env` 60 mp-es értékét is átírtuk, mert az felülírta volna az új alapértéket.

- **Kérés:** ha a teljesítés dátuma jövőbeli, ne foglalkozzunk vele, legyen hibás, és írjuk ki, hogy „teljesítés jövőbeli dátum, nincs hozzá árfolyam”.
- **Az AI kérdései és a döntések (felhasználó):**
  - a számla tárolódik, de kimarad a diagramból és az összesítőből, és a „Hibás fájlok” listában látszik; a teljesítés napja után sem kerül be magától;
  - az azonos fájl újrafeltöltésekor LLM-hívás nélkül, a tárolt adatokból újraértékelünk: ha a nap már elérkezett, a számla bekerül;
  - minden jövőbeli teljesítésű számla kimarad, a forintos is (bár annak nem kellene árfolyam).
- **Eredmény:**
  - validáció: új `info` súlyosság, amely nem teszi ellenőrzendővé a számlát; a jövőbeli teljesítés `future_performance` jelölést kap. Az elemző (`InvoiceAnalyzer`) injektált órát kap;
  - kimutatás: a jelölt számlára nem kérünk árfolyamot; új kizárási ok (`future_performance`), a becsült árfolyam (`estimated`) megszűnt;
  - feltöltés: a már tárolt fájlnál, ha a teljesítés napja elérkezett, a jelölés lekerül, és az eredmény `updated` („Újraértékelve”); különben marad a duplikátum;
  - adatmigráció (`0003`): a már tárolt jövőbeli teljesítésű számlák megkapják a jelölést;
  - felület: „Jövőbeli teljesítés” címke, a csoport magyarázata és a lefedettségi sor az új szabályt írja le; a becsült árfolyamra vonatkozó szövegek törölve;
  - tesztek: jelölés a feldolgozáskor (és a teljesítés napján nincs jelölés), kizárás a kimutatásból MNB-hívás nélkül, újrafeltöltés a teljesítés napja előtt és után, migráció.

### Kimutatás frissítése feldolgozás közben
- **Kérés:** a kimutatás 4 másodpercenként rajzolódjon újra; a felületen látszódjon, hogy a feldolgozás még tart, és sem a kimutatás, sem az összesítő nem végleges. Az ellenőrzendő számlák Bruttó (HUF) cellájában ne legyen külön „nem szerepel a kimutatásban” megjegyzés.
- **Eredmény:** a fájllista továbbra is 1,5 másodpercenként frissül, a kimutatás (diagram, összesítő, számlalista) külön időzítővel 4 másodpercenként; a munka végén egy végleges frissítés jön. A három szakasz fölött „nem végleges” jelzés látszik, amíg feltöltés vagy feldolgozás tart. Az ellenőrzendő állapot csak az Állapot oszlopban látszik. Teszt ellenőrzi, hogy a jelzés mindhárom szakaszban szerepel az oldalon.

### Skálázás: tárolt átváltás, SQL-összesítés, lapozás
- **Kérés:** javítsuk az ismert korlátot: „a számlalista lapozás nélkül töltődik be, a kimutatás minden lekérdezéskor újraszámolódik”.
- **AI-javaslat:** két lehetőséget kínált fel; az (A): az átváltás eredményét a számla sorában tároljuk, az összesítést SQL számolja, a lista szerveroldalon lapoz. A felhasználó előbb kihagyta a kérdést, majd rákérdezett a két lehetőségre, és a magyarázat után az A-t választotta.
- **Eredmény:**
  - adatmodell (`0004`): az `invoices` sor tárolja az átváltást (pénznem, árfolyam, egység, dátum, összeg, hiány oka, kimaradás oka) és egy revíziószámot. A mentés és a jelölés levétele nullázza az átváltást, és növeli a revíziót;
  - kimutatás: csak a még át nem váltott vagy árfolyam nélkül maradt számlákat váltjuk át, egy kötegben, zár alatt. Az eredményt csak akkor mentjük, ha a revízió nem változott közben. A havi bontás, a partnerösszesítő és a lefedettség `GROUP BY` lekérdezés. A kimaradó számlák naplózása a tárolt előző okhoz hasonlít; a memóriabeli állapot megszűnt;
  - API: `GET /api/invoices?group=&offset=&limit=` (legfeljebb 200 sor), csoportonkénti elemszámmal. A `GET /api/report` már nem tartalmazza a teljes listát, csak a kimaradó számlákat (legfeljebb 500-at) és a számukat;
  - felület: szerveroldali csoportszűrés, Előző/Következő lapozó (oldalanként 50 számla); a „Hibás fájlok” lista a kimutatás kimaradó számláiból dolgozik;
  - tesztek: az átváltás nem ismétlődik; újramentés, jelölés levétele és pénznemváltás esetén újra átváltunk; elavult revízióra nem írunk; lapozás és csoportszűrés; az API paraméter-ellenőrzése.
- **AI-tévedés:** az új végpontfájl megírásakor az AI a régit előbb törölte, mint hogy elolvasta volna; a tartalmát a gitből állította vissza és vetette össze.

### Jövőbeli teljesítésű számlák a „Hibás fájlok” listában
- **Kérés:** a jövőben esedékes számlák ne szerepeljenek a hibás fájlok között.
- **Eredmény:** a kimutatás kimaradó számláinak lekérdezése kihagyja a `future_performance` okúakat (a listából és a darabszámból is). Ezek továbbra is kimaradnak a diagramból és az összesítőből, és a számlalista „Jövőbeli teljesítésű számlák” csoportjában, valamint a lefedettségi megjegyzésben látszanak. A tesztet és a README-t frissítettük; Dockerben az 5 jövőbeli tesztszámla mellett a hibás fájlok száma 0.

### Felület egyszerűsítése: jövőbeli számlák, lefedettség, összegzés
- **Kérés:** a jövőbeli számlák Bruttó (HUF) cellájában elég a „–”; az Állapot oszlopban nem kell a „feldolgozáskor a jövőben volt” szöveg; a diagram és az összesítő alatt csak a „Tartalmazza: … számla” sor maradjon; a számlalista alatti „Összesen …: … rendben, … ellenőrzendő” szöveg sem kell.
- **Eredmény:** a jövőbeli számla összegcellája üres jelet kap megjegyzés nélkül; az Állapot oszlop az `info` szintű jelöléseket csak címkeként mutatja; a lefedettség egy sorra rövidült; a lista alatti összegzés megszűnt (az üres lista és az üres csoport üzenete maradt). A nem használt nyelvi kulcsokat töröltük, a README-t frissítettük. Dockerben headless böngészővel ellenőriztük a megjelenítést.
- **Utána:** a diagram címe „Havi cash-flow (fizetési határidő szerint, HUF)” helyett „Bruttó havi kimutatás, fizetési határidő szerint” lett; a pénznem az y tengely feliratán marad.