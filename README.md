# kataster-mcp

MCP server pre kataster nehnuteľností Slovenskej republiky. Umožňuje AI asistentovi alebo jazykovému modelu
v ľubovoľnom klientovi s podporou [Model Context Protocol](https://modelcontextprotocol.io) zistiť o konkrétnom pozemku
čo najviac z verejných zdrojov: hranice a výmeru parcely, list vlastníctva a vlastníkov,
bonitu pôdy, úradnú hodnotu, LPIS, záplavové územia, ochranu prírody, les, sklon terénu, siete a mapu.

*English: an MCP server for the Slovak land registry. An English README is in preparation.*

Údaje majú informatívny charakter. Nie sú výpisom z listu vlastníctva a nedajú sa použiť na právne úkony.

## Čo vie

| Nástroj | Čo vráti |
|---|---|
| `kataster_ku` | katastrálne územie podľa názvu alebo kódu (obec, okres) |
| `kataster_parcela` | parcelu registra C alebo E podľa čísla alebo súradníc: výmera, hranica, referenčný bod |
| `kataster_vlastnici` | vlastníkov a podiely jednej zadanej parcely a číslo listu vlastníctva |
| `kataster_vrstvy` | tematické údaje o parcele (zoznam nižšie), pri plošných vrstvách aj podiel na výmere |
| `kataster_skupina` | rozbor viacerých parciel: celková výmera, spoločné hranice, súvislé celky, susedia |
| `kataster_mapa` | mapu parcely ako PNG, HTML (Leaflet), GeoJSON alebo KML, bez osobných údajov |
| `kataster_lv` | odkaz na výpis z listu vlastníctva na portáli ESKN (výpis si otvoríte sami) |
| `kataster_osoba_navod` | postup krok za krokom, ako si v oficiálnom vyhľadávaní portálu ESKN nájsť listy vlastníctva osoby a uložiť ich náhľady (server pri tom na portál nevolá) |
| `kataster_lv_import` | načítanie výpisu LV alebo náhľadu LV, ktorý ste si uložili (HTML alebo PDF); aj celý priečinok naraz |
| `kataster_osoba_prehlad` | prehľad parciel a podielov jednej osoby zložený z náhľadov LV, ktoré ste si uložili; bez dotazu na portál |
| `kataster_status` | verziu, limity a dnešné využitie kvóty |

Vrstvy v `kataster_vrstvy`: bonitovaná pôdnoekologická jednotka (BPEJ), úradná hodnota pôdy, LPIS, užívateľ pôdy,
záplavy, chránené územia, nadmorská výška, sklon terénu, zastavané územie obce, zakreslené ťarchy, pozemkové úpravy
v katastrálnom území, les a siete (cesty, železnice, elektrické vedenia).

Každá odpoveď uvádza zdroj, čas a to, či údaj prišiel z lokálnej cache. Neoficiálne rozhrania sú označené.

## Čo zámerne nerobí

- **Sám nehľadá podľa osoby.** Vlastníkov zistí len pre parcelu, ktorú zadáte. Ak chcete parcely konkrétnej osoby, vyhľadáte
  ju sami v oficiálnom vyhľadávaní portálu ESKN („Fyzické osoby“), server vás tým prevedie a uložené náhľady listov
  vlastníctva potom načíta a zloží do prehľadu.
- **Nenahrádza súpis nehnuteľností podľa § 69 ods. 9 zákona č. 162/1995 Z. z.**, ktorý vydáva úrad len samotnej osobe.
  Prehľad zložený z náhľadov je informatívny.
- **Neprechádza celé katastrálne územia** kvôli vlastníkom.
- **Neobchádza CAPTCHA.** Keď portál CAPTCHA vyžiada, alebo keď je denná kvóta vyčerpaná, server vám poradí presný ručný
  postup v prehliadači a uložený výpis či náhľad potom načíta.
- **Nezaťažuje portál.** Medzi dotazmi na kataster.skgeodesy.sk čaká aspoň 3 sekundy a nikdy sa nepýta paralelne.
  Vlastníkov zistí najviac pre 50 parciel denne. Tieto limity sa nedajú vypnúť nastavením.

## Inštalácia

Potrebujete [uv](https://docs.astral.sh/uv/getting-started/installation/) (Linux, macOS aj Windows). Server sa spúšťa
ako lokálny proces cez stdio, takže ho pridáte do každého MCP klienta rovnako: príkaz `uvx` s argumentmi
`--from git+https://github.com/braklo/kataster-mcp kataster-mcp`. Väčšina klientov to zapisuje do JSON konfigurácie
v tomto tvare (presné miesto a názov kľúča nájdete v dokumentácii svojho klienta):

```json
{
  "mcpServers": {
    "kataster": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/braklo/kataster-mcp", "kataster-mcp"]
    }
  }
}
```

Napríklad v Claude Code jedným príkazom:

```bash
claude mcp add kataster -- uvx --from git+https://github.com/braklo/kataster-mcp kataster-mcp
```

Potom sa stačí opýtať, napríklad: *„Čo je za pozemok E 503 v katastrálnom území Vyšný Kubín a kto ho vlastní?“*

## Lokálne CLI a skill

Balík poskytuje aj `sk-kataster`. Po inštalácii cez `uv tool install .` alebo v checkout-e
cez `uv run sk-kataster` možno spustiť:

```sh
sk-kataster --json doctor
sk-kataster --json ku resolve 'Vyšný Kubín'
sk-kataster --json lv inspect '/absolutna/cesta/vypis.pdf'
sk-kataster --json lv import '/absolutna/cesta/vypis.pdf' --dry-run
sk-kataster --json lv import '/absolutna/cesta/vypis.pdf'
sk-kataster --json person overview --ico 12345678
sk-kataster --json tools
sk-kataster --json tools kataster_parcela
```

`doctor`, `ku`, `lv inspect`, `lv url`, `lv import` a `person overview` nevolajú portál.
`lv inspect` a `lv import --dry-run` nezapisujú cache. Kontrola a import vracajú predvolene
súhrn; `--full` pridá parcely a osobné údaje vlastníkov. `tools` vracia iba názvy,
`tools NAZOV` schému jedného nástroja. `call NAZOV '{"argument":"hodnota"}'` vykoná
MCP nástroj a podľa zvoleného nástroja môže volať portál alebo zapisovať súbory.
Chyby majú JSON obálku a nenulový exit code. Výstup používa UTF-8 aj na Windows.

CLI a stdio štandardne zdieľajú existujúci dátový adresár `kataster-mcp`.
Premenná `KATASTER_DATA_DIR` ho môže explicitne zmeniť pre oba vstupy;
CLI navyše podporuje `--data-dir`. Predvolená cesta ani ochranné limity sa nemenia.
Voliteľný [skill pre AI klientov](skills/kataster/SKILL.md) opisuje úzke volania,
význam varovaní a prácu s osobnými údajmi. Skill treba pridať do klienta samostatne.

## Čítanie a opakovaný import LV

`tarchy=unknown` znamená chýbajúcu alebo prázdnu časť C. `none` vyžaduje explicitné
„Bez tiarch.“ v prečítanom texte; vzťahuje sa iba na dodaný rozsah. Ostatný obsah zostáva
`see cast_c`. Pole `uplnost` uvádza časti `missing`, `unread` alebo `read` a rozsah
`partial` alebo `unverified`. Prečítanie A/B/C nepotvrdzuje úplnosť dokumentu.

Starší, nedatovaný alebo užší import nenahradí lepší uložený LV. Pri
`ulozenie.stav=preserved_existing` odpoveď opisuje nový vstup, zatiaľ čo prehľad osoby
naďalej používa predchádzajúci záznam. Zmenšenie počtu parciel, stavieb alebo vlastníkov
môže byť aj skutočnou zmenou: vyžaduje ručné preverenie a starší záznam sa nesmie
prezentovať ako potvrdený aktuálny stav. Táto ochrana nie je archív nemenných verzií.
Zdroj dávkového importu odkazuje na vybraný dokument.

IČO musí mať presne osem ASCII číslic, prípadne oddelených medzerami. Ide o kontrolu
formátu, nie pridelenia IČO alebo kontrolnej číslice. Inline HTML má rovnaký limit
20 MiB ako súbor. Zmena parsera zmení kľúč jeho cache verzie; staršie importy sa
automaticky nemigrujú a treba ich znova importovať z pôvodných dokumentov.

SDK je pinované na `mcp[cli]==2.3.0`; stdio a 11 nástrojov zostávajú zachované.
Rozšírenia cez `create_server(policy, extra_tools)` dostávajú `MCPServer` namiesto
`FastMCP`; priame programové volanie SDK vracia `CallToolResult`.

## Osobné údaje

Mená, dátumy narodenia a adresy vlastníkov sa ukladajú len lokálne, do súboru `osoby.sqlite` v dátovom adresári
servera, a po 7 dňoch sa pri ďalšom dotaze načítajú znova. Dátový adresár (presnú cestu vypíše `kataster_status`):

| Systém | Adresár | Ochrana |
|---|---|---|
| Linux | `~/.local/share/kataster-mcp` (alebo `$XDG_DATA_HOME/kataster-mcp`) | práva 0700 / 0600 |
| macOS | `~/Library/Application Support/kataster-mcp` | práva 0700 / 0600 |
| Windows | `%LOCALAPPDATA%\kataster-mcp` | prístup len pre používateľa (predvolené nastavenie Windows) |

Server ich neposiela nikam inam než do odpovede vášmu AI asistentovi; ak model beží v cloude, dostane ich aj jeho
poskytovateľ. Za to, ako ich použijete, zodpovedáte vy (GDPR, katastrálny zákon).

## Zdroje dát

Úrad geodézie, kartografie a katastra SR (INSPIRE, ESKN, ZBGIS, výškový model), Výskumný ústav pôdoznalectva a ochrany
pôdy, Národné lesnícke centrum, Ministerstvo pôdohospodárstva a rozvoja vidieka SR, Slovenský vodohospodársky podnik,
Štátna ochrana prírody SR. Podmienky ich použitia platia aj pri použití cez tento server; zdroj uvádzajte.

## Stav

Verzia 0.1. Otestované s Claude Code na Linuxe. Na Windows a macOS prechádzajú automatické testy, v bežnom používaní
to zatiaľ overené nie je; skúsenosti uvítame v issues.

## Chyby a návrhy

[Issues](https://github.com/braklo/kataster-mcp/issues), po slovensky aj po anglicky. Do issue nedávajte mená
vlastníkov ani výpisy z listu vlastníctva.

## Licencia

Kód: [MIT](LICENSE). Licencia kódu sa nevzťahuje na údaje, ktoré server sprostredkúva.
