# AI-fejlesztési napló

## 2026-11-19 – Felmérés és megvalósítási terv

- **Eszköz és kontextus:** GitHub Copilot; a feladatleírást és a meglévő kezdővázat adtam meg. A munkakönyvtár egy minimális, nem git-es Java/Maven-kezdőváz volt; a feladat technológiát nem kötött ki, ezért a Dockerből futtatható Python/FastAPI megoldást választottam.
- A Desktopon lévő `DEVELOPMENT_RULES.md` fájlt megvizsgáltam, de az külön Java/Spring workspace szabályait tartalmazza; a Python/FastAPI feladatra nem volt érvényes, ezért nem másoltam ezt az idegen projektszabályt a repositoryba.
- **Meghatározó utasítás:** „Készíts egy Docker konténerben futtatható webalkalmazást, amelyben fel lehet tölteni a számlákat, és amely azokból forintos kimutatást készít.”
- **AI-javaslat és döntés:** PDF-szövegkinyerés, tartós SQLite-tárolás, MNB SOAP kliens és egyoldalas dashboard. Elfogadtam ezeket, mert a PDF-ek szövegesek, az adatcsomag mérete mérsékelt, és az SQLite a kért újraindítás utáni megőrzést külön szolgáltatás nélkül biztosítja.
- **LLM-döntés:** opcionális OpenRouter-integrációt választottam változtatható modellnévvel és üres `.env.example` kulcsmezővel. A feladatban szereplő API-kulcsot nem használtam, és nem tettem a repositoryba. A modell kimenetét JSON-ként, kötelező mezőkkel, dátum-, devizanem- és összegellenőrzéssel validálom; az irányt a program az eladó/vevő cégadataiból újraszámolja.
- **Költségcsökkentés és ellenőrzés:** a 100 mellékelt PDF-en végzett első szabályalapú próba 0/100 fájlt dolgozott fel. A mezőcímkék és a külön sorokba tört dátumok vizsgálata után javítottam a többnyelvű címkéket és a felek/adószámok szakaszolását. Az ismételt próba 100/100 fájlnál ki tudta nyerni és validálni a kötelező mezőket, ezért az LLM-et csak szabályalapú hiány vagy irányellenőrzési hiba esetén használom. A referenciaértékekkel való pénzügyi összevetést nem tekintem elvégzettnek.
- A Dockeres 100 fájlos első batch 75 számlát tárolt, mert a számlaszám-kinyerés egy „Invoice No.:” utáni pontot és fizetési megjegyzésekből származó szöveget is érvényes azonosítónak vett; emiatt 25 fájl tévesen duplikátum lett. A hiba a feltöltési státuszok és az issuer+számlaszám kulcsok összevetésekor derült ki. A kinyert számlaszámra szigorú tokenellenőrzést tettem, és a tesztbe bekerült a kétnyelvű mezőcímke és a láblécben szereplő félrevezető mondat esete.

## 2026-11-19 – Adatkinyerés, tárolás és árfolyam

- Elkészült az oldalak közötti PDF-szövegkinyerés, a szabályalapú kinyerő és az opcionális LLM-alapú kinyerés. A nem értelmezhető fájl feldolgozási hibája külön tárolódik, és nem állítja le a többfájlos feltöltést.
- Elkészült a SQLite-adatmodell: fájlhash-alapú ismételt feltöltés elleni védelem, kiállító+számlaszám egyediség, számlarekordok, napi MNB-árfolyamok és lekérési időszakok.
- Az árfolyamkiválasztás a teljesítés dátumát használja, és a jegyzés nélküli napokra az utolsó korábbi MNB-adatot választja. A SOAP-on kapott XML-szöveget külön kell feldolgozni; a tesztben az árfolyam-lekérést mockolom, így a teszt nem függ külső szolgáltatástól. A lekérési tartományok csak a korábban le nem fedett napokat tartalmazzák, hogy ne kérdezzen le az alkalmazás azonos nap/deviza adatot ismét.

## 2026-11-19 – Kimutatás, tesztek és dokumentáció

- Elkészült az SVG-alapú havi diagram, az adószám szerinti partnerösszesítő, a részletes számlalista és a fájlonkénti hibajelzés. A felületről eltávolítottam a külső betűkészlet-függőséget is.
- Automatizált tesztek készültek a mezőkinyerésre és lokalizált formátumokra, az escape-elt SOAP-válasz és árfolyam-egység feldolgozására, az árfolyam dátumának/cache-elésének kiválasztására, valamint az aggregálásra és az üres hónapra.
- A Dockeres feltöltési próbában az első HUF-számla hibát adott, mert az ISO dátumszöveg dátumobjektumként került az adatbázisba írási útvonalra. A konténer naplójából azonosítottam a típuskülönbséget, a dátumot a feldolgozás előtt explicit módon parse-olom, majd újra lefuttattam a feltöltés, ismételt feltöltés és konténer-újraindítás utáni megőrzés próbáját.
- **Ellenőrzés eredménye:** 6 automatizált teszt sikeres; a szabályalapú kinyerés mind a 100 fájlnál validálható mezőket adott, és a javított Docker batch mind a 100-at feldolgozta duplikáció nélkül. A Docker HTTP-próba az ismételt feltöltés idempotenciáját és az adat konténer-újraindítás utáni megőrzését is ellenőrizte. Az MNB SOAP-formátumot tesztek mockkal és egy élő lekéréssel is ellenőriztem. A helyi 8080-as port foglalt volt, ezért a Docker-próbákhoz alternatív `APP_PORT` értéket használtam. A PDF-ek pénzügyi eredményeit referenciaösszegekkel továbbra sem vetettem össze.
- A README leírja a Dockeres indítást, a feldolgozási módokat, az MNB-integrációt és a korlátokat. Az AI-javaslatokat a tesztekkel és a cégadat-alapú irányvalidációval ellenőrzöm; az AI-folyamat naplója nem állít olyan kézi vagy referenciaadat-ellenőrzést, amely még nem történt meg.
