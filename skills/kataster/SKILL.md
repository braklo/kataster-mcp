---
name: kataster
description: Kontrola a import uloženého slovenského LV (HTML/PDF), katastrálne územia a prehľad osoby z vlastných importov cez sk-kataster CLI alebo Kataster MCP.
---

Použi nainštalovaný `sk-kataster`. Over `command -v sk-kataster` a `sk-kataster --json doctor`. Lokálne operácie nepotrebujú prihlasovanie.

Začni úzkym príkazom; celé MCP schémy načítaj iba podľa potreby:

```sh
sk-kataster --json ku resolve 'Vyšný Kubín'
sk-kataster --json lv inspect '/absolutna/cesta/vypis.pdf'
sk-kataster --json lv import '/absolutna/cesta/vypis.pdf' --dry-run
```

`lv inspect` nič neukladá. Po požiadavke používateľa na import použi `lv import` bez `--dry-run`; osobné údaje sa ukladajú do lokálnej cache. Výsledok je predvolene súhrn, `--full` vracia aj vlastníkov a položky. `person overview --name 'Meno Priezvisko'` alebo `--ico 12345678` pracuje iba s importovanými LV. Nejde o úplný súpis majetku osoby.

Úspech má `{data, zdroje, varovania}`, chyba `{chyba, zdroje, varovania}` a nenulový exit code. `tarchy=unknown` znamená chýbajúcu alebo neprečítanú časť C. `none` vyžaduje explicitné „Bez tiarch.“ v prečítanom texte a platí iba pre dodaný rozsah. `see cast_c` vyžaduje prečítanie obsahu cez `--full`, nie automatický záver o druhu ťarchy. `uplnost` opisuje čitateľnosť častí, nepotvrdzuje úplnosť výpisu.

`ulozenie.stav=preserved_existing` znamená, že vstup nenahradil uložený LV. Výsledok importu opisuje nový vstup, prehľad osôb naďalej číta starý záznam. Pri zmenšenom rozsahu treba preveriť úplnosť alebo skutočnú zmenu vlastníctva; netvrď, že starší záznam predstavuje aktuálny stav.

Pre ďalšie nástroje použi `sk-kataster --json tools`, potom `tools NAZOV` a až potom `call NAZOV '{"argument":"hodnota"}'`. `call` môže volať portál, importovať údaje alebo zapisovať mapy; vyber iba operáciu vyžiadanú používateľom. Pri dostupnom MCP možno rovnaké nástroje volať priamo. Nekombinuj duplicitné importy oboma cestami.

CAPTCHA rieši používateľ. Neobchádzaj blokáciu ani kvóty. Pri zlyhaní portálu neopakuj hromadne volania. Zdroj, dátum údajov, rozsah a varovania zahrň do záverov. Prevádzkové údaje a živé LV neukladaj do repozitára.
