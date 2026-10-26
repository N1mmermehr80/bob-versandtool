# BOB Sendcloud-Versand

Holt offene Aufträge direkt aus Speed4Trade CONNECT (oder liest den S4T-Versandexport `GLS_*.csv`), legt ausgewählte Aufträge als DHL-Sendung bei Sendcloud an und meldet die Sendungsnummern an Speed4Trade zurück (bzw. exportiert sie als CSV).

**Programm:** Ordner mit `BOB-Sendcloud-Versand.exe` und Unterordner `_internal` (aus `BOB-Sendcloud-Versand.zip` im Release), z. B. `C:\BOB-Versandtool\`. Keine Installation nötig, gestartet wird die exe im Ordner (am besten per Verknüpfung). Die Ordner-Variante startet in rund 1 Sekunde, weil sie sich nicht bei jedem Start entpacken muss.

## Ablauf

1. **Aus S4T abholen:** Holt alle Aufträge mit Status 130 „bezahlt, Versand offen“ aus Speed4Trade. Alternativ **CSV importieren…** für den S4T-Export.
2. **Prüfen:** Farbe der Zeile beachten (siehe unten). Per Doppelklick lassen sich Adresse, Paketanzahl und Gewicht ändern.
3. **Auswählen:** Kästchen in der ersten Spalte anklicken (oder Leertaste), das Kästchen im Spaltenkopf wählt alle angezeigten aus bzw. ab. Alternativ „Alle auswählen“. Fehlerhafte und zu prüfende Aufträge werden dabei nicht automatisch erfasst. Versendete Aufträge zeigen einen grünen Haken.
   - **Sortieren:** Klick auf eine Spaltenüberschrift sortiert aufsteigend (▲), erneuter Klick absteigend (▼), ein dritter stellt die ursprüngliche Reihenfolge wieder her. Leere Werte stehen immer unten.
   - **Einzeln senden:** „▸ Senden“ in der Zeile oder „Diesen Auftrag senden“ im Detailbereich sendet genau diesen Auftrag (mit derselben Sicherheitsabfrage).
4. **🚚 Senden:** Ausgewählte Aufträge an Sendcloud senden. Die Sendungen werden bei DHL angemeldet, die Labels liegen danach zum Drucken im Sendcloud-Panel.
5. **🖨 Labels drucken:** Die Label-PDFs werden beim Senden automatisch ins Programm geladen. Hier lassen sie sich gesammelt drucken:
   - **Drucken…** öffnet den Windows-Druckdialog (Drucker, Eigenschaften, Exemplare) und druckt direkt, ohne PDF-Programm. Der gewählte Drucker wird je Format (A6/A4) gemerkt und beim nächsten Mal vorausgewählt. Mit „Ohne Druckdialog direkt drucken“ in den Einstellungen geht der Druck sofort an diesen Drucker.
   - **Als PDF öffnen** zeigt das Sammel-PDF wie bisher im PDF-Programm.
   - Bricht man den Druckdialog ab, bleiben die Labels als ungedruckt markiert.
   - Auswahl: alle ungedruckten, nur angehakte oder die markierte Zeile
   - Reihenfolge im Sammel-PDF: so, wie die Aufträge vor dem Senden angehakt wurden (auch über mehrere Sendevorgänge). Aufträge ohne Häkchen-Reihenfolge, z. B. von Kollegen gesendet, kommen danach in Listenreihenfolge.
   - Format: A6 für den Etikettendrucker oder A4 für den Bürodrucker (mehrere Labels pro Seite)
   - Sendcloud liefert DHL-Labels in einem Sonderformat (~69 × 140 mm). Das Tool setzt sie automatisch formatfüllend auf echtes A6 (105 × 148 mm).
6. **💾 Exportieren:** Sendungsnummern als CSV (`Auftragsnummer;Sendungsnummer`) speichern.

**✔ Manuell versendet:** Markiert einen Auftrag ohne Sendcloud als versendet, mit oder ohne Sendungsnummer, z. B. bei Abholung. Mit Sendungsnummer erscheint er im Export. Ein zweiter Klick nimmt die Markierung zurück.

**🗑 Löschen** (oder Entf-Taste): Entfernt die angehakten Aufträge aus der Liste, ohne Häkchen die markierte Zeile. Das Label wird dabei **nicht** in Sendcloud storniert. Lädst du später eine Exportdatei, die gelöschte Aufträge enthält, fragt das Tool, ob sie wieder angezeigt werden sollen.

**🔍 Schnellsuche** (Strg+F): Sucht beim Tippen in Auftragsnummer, Name, Firma, Adresse, PLZ, Ort, E-Mail, Artikel und Sendungsnummer. Mehrere Wörter werden UND-verknüpft, z. B. „mazda frunk“. Mit Enter oder Pfeil runter springst du in die Liste, Esc leert die Suche.
- **Filter-Reiter** neben der Suche, jeweils mit Live-Zähler: Alle, Offen, Prüfen, Fehler, Ausgewählt, Versendet, Ungedruckt, Nicht exportiert. Der aktive Reiter ist gelb unterstrichen.
- **„Alle auswählen“** gilt bei aktiver Suche oder aktivem Filter nur für die angezeigten Aufträge.

**Rechtsklick auf eine Zeile:** Bearbeiten, manuell versendet, Label öffnen, als „nicht gedruckt“ markieren, löschen.

**Arbeitsliste:** Die Liste wird nach jeder Änderung automatisch gespeichert und beim nächsten Start wiederhergestellt.

**Import einer neuen Exportdatei:** Die neuen Aufträge werden der Liste **hinzugefügt**, nichts wird ersetzt.
- Aufträge, die schon in der Liste stehen, behalten ihren Stand und ihre Bearbeitungen.
- **Erledigte** Aufträge werden dabei automatisch aus der Liste entfernt. Erledigt heißt: versendet, Label gedruckt (falls vorhanden) und Sendungsnummer exportiert (falls vorhanden).
- Erledigte und gelöschte Aufträge kommen bei späteren Importen nicht wieder. Bei gelöschten fragt das Tool nach.
- Nach dem Import zeigt eine Meldung, wie viele Aufträge hinzugefügt, schon vorhanden, übersprungen und entfernt wurden.

### Zeilenfarben

Automatisch korrigierte Aufträge bleiben weiß, Details dazu stehen im Hinweisbereich. Eingefärbt werden nur Zeilen, bei denen etwas zu tun ist oder die erledigt sind.

| Farbe | Bedeutung |
|---|---|
| orange | Gewicht nur geschätzt (Radsatz oder Warenwert über 500 €) – bitte prüfen, wird von „Alle auswählen“ nicht erfasst |
| rot | nicht sendbar |
| grün | Label erstellt |
| blau | war schon in Sendcloud vorhanden |
| lila | manuell versendet |

## Speed4Trade-Anbindung

Einrichten unter **Einstellungen → Speed4Trade-Anbindung…**: Server-Adresse, Webservice-Benutzer und Passwort. Liegt eine `.env` mit `S4T_IP`, `S4T_Benutzername` und `S4T_Passwort` neben der exe, werden die Werte beim ersten Start übernommen.

- **Abholen:** alle Aufträge im eingestellten Status (Standard 130 = bezahlt, Versand offen). Optional lässt sich auf ein Auftragskennzeichen einschränken. Aufträge, die schon in der Liste stehen, bleiben unverändert.
- **Automatischer Abgleich:** Beim Start und danach alle 5 Minuten holt das Tool im Hintergrund neue Aufträge, ohne Rückfrage. Aufträge, die in Speed4Trade nicht mehr offen sind (z. B. dort von Hand versendet oder storniert), verschwinden aus der Liste, sofern sie hier noch unbearbeitet sind. Angehakte, gesendete oder gerade gesendete Aufträge bleiben stehen. Das Ergebnis steht in der Statuszeile. Während Senden oder Drucken läuft, wartet der Abgleich auf den nächsten Durchlauf.
- **Rückmeldung:** Nach dem Senden meldet das Tool den Versand mit Sendungsnummer (DHL) an Speed4Trade. Speed4Trade setzt den Auftrag dann auf „versendet“, beim nächsten Abholen kommt er nicht wieder. Gemeldete Aufträge gelten als exportiert, ein CSV-Export ist für sie nicht mehr nötig.
- **Fehler bei der Rückmeldung:** Das Label bleibt gültig. Der Auftrag zeigt den Fehler in den Hinweisen, die Meldung lässt sich per **Rechtsklick → An Speed4Trade als versendet melden…** nachholen. Darüber lassen sich auch manuell versendete Aufträge mit Sendungsnummer melden.
- **Gewicht:** Speed4Trade liefert in den Aufträgen kein Gewicht. Es gelten die Regeln unten (Erfahrungswerte, Radsatz-Regel, sonst 2,01 kg).
- **Zertifikat:** Der Server nutzt ein selbst ausgestelltes Zertifikat. Beim ersten „Verbindung testen“ zeigt das Tool den Fingerabdruck zur Bestätigung. Danach gehen die Zugangsdaten nur an genau diesen Server. Ändert sich das Zertifikat, bricht das Tool ab, ohne etwas zu senden, und fragt beim nächsten Test erneut.
- Server, Status, Mandant, Kennzeichen und der bestätigte Fingerabdruck gelten für alle Arbeitsplätze (gemeinsamer Ordner). Benutzer und Passwort bleiben pro PC.

## Mehrere Arbeitsplätze (gemeinsame Liste)

Ohne Server und ohne Adminrechte: Die Liste liegt in einem **Netzwerkordner**, auf den alle schreiben dürfen.

**Einrichten** (an jedem PC einmal):
1. Unter **Einstellungen → Gemeinsamer Ordner** den Pfad eintragen, z. B. `\\server\freigabe\Versand`. Alle müssen **denselben** Pfad verwenden.
2. Das Tool neu starten. Unten links steht dann „Gemeinsame Liste · angemeldet als <Windows-Benutzer>“.
3. Wer zuerst umstellt, wird gefragt, ob die lokale Liste in den Ordner übernommen werden soll.

**So arbeitet die gemeinsame Liste:**
- **Ablage:** Jeder Auftrag ist eine eigene Datei mit Versionsnummer. Änderungen der Kollegen erscheinen nach etwa 4 Sekunden.
- **Senden:** Das Tool reserviert den Auftrag vorher im Ordner. Sendet ein Kollege gerade, steht in der Zeile „wird gesendet… (Name)“. Der Auftrag ist dann für alle anderen gesperrt, und es entstehen keine doppelten Labels. Eine Reservierung verfällt nach 15 Minuten, z. B. nach einem Absturz.
- **Gleichzeitige Änderungen:** Ändern zwei Leute denselben Auftrag gleichzeitig, gewinnt der zuerst Gespeicherte. Der andere bekommt einen Hinweis und sieht den neuen Stand.
- **Import, Löschen, Erledigt:** gelten für alle.
- **Labels:** liegen im Ordner (`labels\`). Jeder kann alle Labels am gemeinsamen Drucker drucken.
- **Protokoll:** je Benutzer in `protokoll\<Benutzer>.csv`.
- **Netzlaufwerk weg:** Unten erscheint ein roter Hinweis. Senden ist dann nicht möglich, Änderungen werden nachgereicht, sobald der Ordner wieder erreichbar ist.

**Pro PC einzeln:**
- Sendcloud-Zugangsdaten
- Absender und weitere Einstellungen
- die eigene Auswahl (Häkchen)

**Netzlaufwerk:** Auf Netzlaufwerken (z. B. `U:\…`) dauert jedes Öffnen einer Datei spürbar (~0,3 s). Das Tool liest deshalb parallel und beim Abgleich nur geänderte Dateien. Bekannte Aufträge werden beim Abholen gar nicht erst in den Ordner geschrieben.

## Automatische Korrekturen

- Land steht im Export in `STATE CODE` → wird nach `COUNTRY CODE` übernommen.
- Abgeschnittene eBay-E-Mails (`…@members.e`) werden vervollständigt.
- Hausnummer wird aus Straße oder Adresszusatz getrennt.
- Gutschein- und Rabattzeilen kommen nicht ins Paket, ihr Betrag wird aber vom Warenwert abgezogen.
- Mehrere Exportzeilen eines Auftrags werden zu einer Sendung zusammengefasst.

## Gewicht

Das Gewicht wird in dieser Reihenfolge bestimmt:

1. Gewicht aus dem Export, sofern bei allen Artikeln gepflegt. Artikelgewichte unter 50 g gelten als Platzhalter. Fehlt ein Artikelgewicht, ist das Auftragsgewicht (`ORDER WEIGHT`) zu niedrig und wird nur noch als Untergrenze genutzt.
2. Erfahrungswert aus früheren Sendcloud-Sendungen desselben Artikels (letzte 120 Tage). Er wird nur übernommen, wenn er schwerer als das Standardgewicht ist oder mehrere Pakete ergibt.
3. Radsätze (Kompletträder, WKR, Reifengröße im Namen): 4 Pakete à 20 kg.
4. Sonst das Standardgewicht von 2,01 kg (in den Einstellungen änderbar).

Bei mehreren Paketen wird je Paket eine eigene Sendung mit derselben Auftragsnummer angelegt.

## Sicherheit

- **Doppelte Labels:** Vor jedem Anlegen prüft das Tool, ob es zur Auftragsnummer schon eine aktive Sendung gibt. Wenn ja, legt es keine neue an und übernimmt die vorhandene Sendungsnummer.
- **Ablageort:** Unter `%APPDATA%\BOB-Sendcloud-Versand\` liegen:
  - Einstellungen
  - Arbeitsliste (`arbeitsliste.json`)
  - Label-PDFs (`labels\`) und Sammeldrucke (`sammeldruck\`)
  - Protokoll aller Sendungen (`sendungen_log.csv`)
  - Erfahrungswerte
- **Zugangsdaten:** Die Sendcloud-Schlüssel und das Speed4Trade-Passwort werden mit Windows-DPAPI verschlüsselt gespeichert und sind nur für den eigenen Windows-Benutzer lesbar. Beim ersten Start werden sie aus einer `.env` neben der exe übernommen, falls vorhanden.

## Updates und neue Versionen (GitHub)

Das Tool prüft beim Start und danach alle 6 Stunden, ob es auf GitHub eine neuere Version gibt. Wenn ja, erscheint oben ein gelber Hinweis mit **„Jetzt aktualisieren“**:
- Das Paket (ZIP) wird heruntergeladen, mit der Prüfsumme von GitHub verglichen, nach `_update\neu` im Programmordner entpackt und auf Vollständigkeit geprüft.
- Das Tool beendet sich. Die neue Version wartet, bis es zu ist, sichert die alte als `*.alt`, kopiert sich in den Programmordner und startet. Geht dabei etwas schief (z. B. eine Datei ist gesperrt), stellt sie die alte Version wieder her und startet diese.
- `*.alt` und `_update` werden beim nächsten Start aufgeräumt.
- **Umstellung von der Einzel-exe (bis 1.0.6):** Diese Versionen aktualisieren sich zunächst auf die neue Einzel-exe. Die bietet dann „Schnellere Programmversion verfügbar“ an und stellt auf die Ordner-Variante um, im selben Ordner.
- Adminrechte sind nicht nötig, solange der Programmordner für den Benutzer beschreibbar ist (z. B. `C:\BOB-Versandtool`, nicht `C:\Program Files`).
- Die Arbeitsliste bleibt erhalten.

Unter **Einstellungen → Updates** stehen die installierte Version, das Repository und „Jetzt nach Updates suchen“.

### Einmalig einrichten
1. Auf GitHub ein Repository anlegen. Es muss **öffentlich** sein, sonst kann das Tool ohne Zugangsdaten keine Updates abrufen. Der Code enthält keine Zugangsdaten.
2. **Nur den Ordner `tool\`** hochladen, nicht den Projektordner darüber. Dort liegen `.env` mit Passwörtern und `sample\` mit Kundendaten. Die `.gitignore` schließt CSV, PDF, JSON und `.env` zusätzlich aus.
3. In `versand_tool.py` bei `UPDATE_REPO` das Repository eintragen, hier `"N1mmermehr80/bob-versandtool"`. Alternativ trägt jeder es unter Einstellungen → Updates ein.

### Neue Version veröffentlichen
```
git commit -am "Beschreibung der Änderung"
git tag v1.1.0
git push --tags
```
GitHub Actions (`.github/workflows/release.yml`) baut daraus automatisch die Ordner-Variante als ZIP und übergangsweise die Einzel-exe, prüft das Paket, setzt die Versionsnummer aus dem Tag und veröffentlicht ein Release. Ein Testlauf ohne Release (Actions → Release → Run workflow) stellt ZIP und exe drei Tage als Download bereit. Wenige Minuten später bieten alle Arbeitsplätze das Update an. Die Versionsnummer muss dabei steigen, z. B. v1.0.0 → v1.1.0.

## Lokal bauen

`build.bat` ausführen. Voraussetzung: Python 3 mit `pip install pyinstaller pypdf pypdfium2==5.14.0`. Hinweis: Mit der Python-Version aus dem Microsoft Store fehlt in der Ordner-Variante teils `python313.dll` – für Releases baut deshalb GitHub.

Test ohne Oberfläche und ohne Sendungen anzulegen:

```
python versand_tool.py --selftest datei.csv [--history]
```
