---
name: kataster
description: Slovenský kataster cez sk-kataster CLI alebo Kataster MCP. Kontrola a import uloženého výpisu alebo náhľadu LV (HTML/PDF), katastrálne územia, prehľad osoby z vlastných importov.
---

Použi nainštalovaný `sk-kataster` a over ho príkazom `sk-kataster --json doctor`. Ukáže verziu, adresáre a dnešné využitie dennej kvóty na vlastníkov. Lokálne operácie nepotrebujú prihlásenie.

Začni úzkym príkazom. Schému nástroja načítaj, až keď ju potrebuješ:

```sh
sk-kataster --json ku resolve 'Vyšný Kubín'
sk-kataster --json lv inspect '/absolutna/cesta/vypis.html'
sk-kataster --json lv import '/absolutna/cesta/vypis.html' --dry-run
```

`lv inspect` ani `--dry-run` nič neukladajú. `lv import` bez `--dry-run` použi až vtedy, keď o import používateľ požiada. Osobné údaje sa ukladajú len do lokálnej cache. Výsledok je predvolene súhrn, vlastníkov a parcely vráti `--full`. `person overview` pracuje len s importovanými LV a nie je to úplný súpis majetku osoby.

Úspešná odpoveď má tvar `{data, zdroje, varovania}`, chyba `{chyba, zdroje, varovania}` s nenulovým exit kódom.

Ťarchy (`tarchy`) čítaj takto. Pri výpise LV znamená `none` výslovné „Bez tiarch.“ v časti C. Pri náhľade LV znamená `none`, že časť C je na stránke, ale jej tabuľka je prázdna; pole `tarchy_poznamka` to uvádza a pri závere to spomeň. `unknown` znamená, že časť C chýba alebo je prázdna, takže neprítomnosť tiarch nie je potvrdená. Pri `see cast_c` si obsah prečítaj cez `--full` a o druhu ťarchy nič nedomýšľaj. `uplnost` hovorí, ktoré časti boli v dokumente a či ide o čiastočný výpis; úplnosť výpisu nepotvrdzuje.

Opakovaný import toho istého LV: novší dátum „Údaje platné k“ uložený LV vždy nahradí. Keď z neho ubudli vlastníci, parcely alebo stavby, varovanie uvedie ich počet. Môže to byť predaj, ale aj neúplne uložený súbor, preto to používateľovi povedz. Starší alebo nedatovaný dokument, čiastočný výpis namiesto úplného a pri rovnakom dátume dokument s menším obsahom sa neuložia (`ulozenie.stav=preserved_existing`). Odpoveď vtedy opisuje nový vstup, ale prehľad osoby naďalej používa uložený LV.

Ďalšie nástroje nájdeš cez `sk-kataster --json tools`, potom `tools NAZOV` a až potom `call NAZOV '{"argument":"hodnota"}'`. `call` môže ísť na portál, importovať údaje alebo zapisovať mapy, preto spúšťaj len to, o čo používateľ požiadal. Pre `call` platí tá istá pauza medzi dotazmi a ten istý denný strop ako pre MCP server, spoločne pre všetky procesy. Stav limitov je na pevnom mieste, takže ho nepresunie ani `--data-dir`, ani `KATASTER_DATA_DIR`. Keď je k dispozícii MCP, volaj tie isté nástroje priamo a ten istý import nerob oboma cestami.

CAPTCHA rieši používateľ v prehliadači. Blokáciu ani kvótu neobchádzaj a pri zlyhaní portálu volania hromadne neopakuj. Do záverov zahrň zdroj, dátum údajov, rozsah a varovania. Živé LV ani prevádzkové údaje neukladaj do repozitára.
