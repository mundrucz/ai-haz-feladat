# Számlafeldolgozó és cash-flow kimutatás

Dockerből futtatható webalkalmazás a Nordwind Solutions Kft. PDF-számláinak feldolgozására. A feltöltött számlák mezőit eltárolja, a külföldi devizákat a teljesítés napján érvényes MNB-árfolyamon forintra váltja, majd havi cash-flow diagramot és partnerenkénti összesítést készít.

## Indítás

Docker és Docker Compose szükséges; a forráskód helyi Python- vagy Java-környezetet nem igényel.

```bash
docker compose up --build
```

Az alkalmazás címe: <http://localhost:8080>. Ha a 8080-as port foglalt, az `APP_PORT` változóval másik helyi port választható. A feltöltött adatok az `invoice-data` Docker-kötetben maradnak meg a konténer újraindítása után is.

Az OpenRouter-alapú kinyerés opcionális. Használatához másold a `.env.example` fájlt `.env` néven, majd add meg az `OPENROUTER_API_KEY` értékét. A modellt az `OPENROUTER_MODEL` változóval lehet beállítani. A feladatban megadott kulcsot ez a repository nem tartalmazza. Az alkalmazás először a többnyelvű szabályalapú kinyerőt futtatja, és csak akkor fordul a modellhez, ha a szabályok nem tudják a kötelező mezőket kinyerni. Így a már kezelt sablonok nem fogyasztanak LLM-hívást. Kulcs nélkül az ismeretlen vagy kétértelmű szerkezetű fájlról fájlszintű hibát jelez.

## Architektúra

- **FastAPI** biztosítja a feltöltési és kimutatási API-t, valamint kiszolgálja a statikus webes felületet.
- **PyMuPDF** olvassa ki a szöveget az összes PDF-oldalról; a fájlnevek nem befolyásolják a számlaadatok kinyerését.
- Az adatokat és az MNB-árfolyamokat **SQLite** tárolja. A számlafájl SHA-256 lenyomata megakadályozza ugyanazon feltöltés ismételt feldolgozását, a kiállító normalizált azonosítója és a számlaszám pedig megakadályozza ugyanazon számla duplikálását eltérő fájlként is.
- A böngészőoldal saját SVG-vonaldiagramot, partnerösszesítőt, számlalistát és fájlonkénti hibajelzést jelenít meg; külső diagramkönyvtár nem szükséges.

## Adatkinyerés és ellenőrzés

Ha a szabályalapú kinyerés nem tudja kinyerni a kötelező mezőket, az OpenRouter-kulcs esetén a konfigurált modell (alapértelmezésben `openai/gpt-4o-mini`) kinyeri őket. A teljes PDF-ből kinyert szöveget egyszer küldi el feldolgozáskor, a választ pedig JSON-ként ellenőrzi. A kötelező mezők meglétét, a dátumok és a bruttó összeg értelmezhetőségét, a devizanemet, valamint a cég adószámából/adatából számított irányt ellenőrzi. Az irányt nem az LLM döntése határozza meg: az alkalmazás újraszámolja, hogy a Nordwind a kiállító vagy a vevő. Az eredeti összeg és az alkalmazott árfolyam is látható, így a kimutatás ellenőrizhető.

Kulcs nélkül a kinyerő gyakori magyar, angol, német, francia, spanyol, olasz és lengyel mezőcímkékre és számformátumokra támaszkodik. Ismeretlen vagy kétértelmű szerkezet esetén nem találgat: az adott fájlt hibásként jelöli, a többi fájl feldolgozása folytatódik. A szkennelt, szöveget nem tartalmazó PDF nem támogatott.

## MNB-árfolyam és kimutatás

HUF-számlához az árfolyam 1. EUR, USD, GBP és PLN esetén az alkalmazás az MNB `GetExchangeRates` SOAP műveletét hívja a `http://www.mnb.hu/arfolyamok.asmx` végponton. Kezdetben a teljesítés napjáig 14 napos visszatekintő időszakot kér le, később csak a még le nem fedett időszakot; a SOAP-válasz belső XML-jét külön feldolgozza, és figyelembe veszi az árfolyamhoz tartozó egységet. Ha a teljesítés napján nincs jegyzés, a legutolsó korábbi jegyzett napot használja. A lekérési időszakok és a kapott napi árfolyamok adatbázisba kerülnek, ezért egy már lefedett nap/deviza ismételt feldolgozásakor nem indít újabb lekérést. HUF-összeg kerekítése számlánként, egész forintra, `0,5`-től felfelé történik.

A diagram a fizetési határidő hónapja szerint összesít; a két számlás hónap közötti üres hónapot nullával jeleníti meg. A táblázat partnert adószám szerint von össze, és külön mutatja a bejövő és kimenő összegeket.

## Fejlesztés és teszt

Teszt futtatása Python 3.12-es fejlesztői környezetben:

```bash
pip install -r requirements.txt
pytest -q
```

A tesztek az adatkinyerést és lokalizált dátum-/összegformátumokat, a hétvégi árfolyamkiválasztást és a lekérdezési cache-t, valamint a havi és partnerenkénti aggregálást fedik le.

## Ismert korlátok, továbbfejlesztés

- A kulcs nélküli kinyerő szabályalapú, ezért új számlasablonhoz címkemintákat kell hozzáadni; OpenRouter-kulccsal a modell általánosabban kezeli a formátumokat, de a kinyert adatot továbbra is érdemes felhasználói ellenőrzéssel megerősíteni.
- Az árfolyam-lekéréshez a konténernek internetelérésre van szüksége. MNB-elérhetetlenségnél a nem HUF-számla hibaállapotba kerül, és később újra feltölthető/újrapróbálható.
- Továbbfejlesztés lehet a felülvizsgálati és javítási felület, a számlánkénti adatexport, valamint árfolyam-időszakok nagyobb csoportokban történő előzetes lekérése.
