# optimized_solution

Eigenständige Alternative für die Atira-RFQ-Challenge. Die bestehenden Agenten im Repository werden nicht verwendet und nicht verändert.

## Architektur

Ein Modell steuert die Browser. Es sieht eine kompakte Seitenbeobachtung und antwortet mit höchstens vier Aktionen. Code führt diese Aktionen aus, liest ein vom Modell gewähltes Dokument oder blättert ein vom Modell gewähltes Resultat. Die extrahierten Fakten landen in einem typisierten Ledger. `kernel.py` wendet die Angebotsregeln an und rechnet in `Decimal` mit HALF_UP. Fehlt etwas, wird genau diese Lücke noch einmal gesucht. Danach trägt derselbe Tender-Browser das Angebot ein; das Modell ordnet Felder Schlüsseln zu, das Programm tippt die berechneten Werte. Die TAN kommt erst, wenn das Formular sie verlangt.

Verworfen:

- Ein freier Agent pro Website plus ein zweites Modell, das die Zahlen noch einmal entscheidet. Die Regeln sind deterministisch, ein zweiter Entscheider kostet Tokens und kann richtig extrahierte Fakten wieder verfälschen.
- LangGraph oder ein Knowledge Graph. Die Abhängigkeiten sind ein Ledger und drei Wartepunkte (Referenz, Kataloggruppe, TAN), kein Graph-Framework.
- Clef als Veto vor jeder Extraktion. Ein zusätzlicher Hop kostet Laufzeit, und ein Fehlveto gefährdet die Korrektheit.

## Start

```powershell
cd optimized_solution
uv sync
uv run playwright install chromium
```

In einem zweiten Terminal `atira models proxy` laufen lassen und die Scouting-Logins laden. Dann:

```powershell
uv run python agent.py
```

Tests, ohne Browser und ohne Modell:

```powershell
uv run --no-project python -m unittest discover -s tests -v
```

Ein Sandbox-Lauf, keine Leaderboard-Submission:

```powershell
atira test-run ./optimized_solution --challenge rfq
```

`OPT_LOCAL_BUDGET_S` begrenzt einen lokalen Lauf (Standard 1200 Sekunden).

## Was funktioniert und was nicht gemessen ist

Die Kalkulation, das Ledger, der Aktionsloop und die Metriken sind mit Unit-Tests auf erfundenen Zahlen geprüft. Ein Lauf gegen die öffentlichen Websites oder die Atira-Sandbox wurde nicht gestartet. Deshalb gibt es keine gemessene Quality, keine Plattformkosten und keinen Vergleich der Laufzeit mit den bestehenden Agenten.

Die lokalen Metriken (Laufzeit, geschätzte Qwen-Kosten, Aufrufe, Tokens, Browseraktionen, Wiederholungen, fehlgeschlagene Tasks, Phasendauern, Submission) stehen am Ende von `agent.py` als JSON in der Standardausgabe. Die Plattform misst Kosten und Zeit selbst.

## Grenzen

- Die Extraktion bleibt ein Modellaufruf. Unbekannte Dokumente können falsch strukturiert werden; die Recovery-Runde fragt nur die Lücken, die der Kernel benennen kann.
- Mehrere exakt passende Materialien: es wird die kleinste Materialnummer genommen und eine Warnung geschrieben.
- Schatten-DOM und iframes werden mitgelesen. Ein PDF, das nur in einem Viewer gerendert ist und keinen Textlayer hat, bleibt für die Extraktion leer.
- Lokales Chromium muss einmal installiert werden. In der Sandbox werden die von Atira gestarteten Browser über CDP verbunden.
