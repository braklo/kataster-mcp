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
| `kataster_vlastnici` | vlastníkov a podiely jednej zadanej parcely a číslo listu vlastníctva; na požiadanie (`detail_stavieb`) aj detail stavby na nej: LV stavby, druh a vlastníkov (každá stavba je ďalší dotaz do denného limitu; pri bytovom dome vlastníkov bytov nevracia, namiesto toho navedie, ako si uložiť a naimportovať list vlastníctva domu) |
| `kataster_vrstvy` | tematické údaje o parcele (zoznam nižšie), pri plošných vrstvách aj podiel na výmere |
| `kataster_skupina` | rozbor viacerých parciel: celková výmera, spoločné hranice, súvislé celky, susedia |
| `kataster_mapa` | mapu parcely ako PNG, HTML (Leaflet), GeoJSON alebo KML, bez osobných údajov |
| `kataster_lv` | odkaz na výpis z listu vlastníctva na portáli ESKN (výpis si otvoríte sami) |
| `kataster_osoba_navod` | postup krok za krokom, ako si v oficiálnom vyhľadávaní portálu ESKN nájsť listy vlastníctva osoby a uložiť ich náhľady (server pri tom na portál nevolá) |
| `kataster_lv_import` | načítanie výpisu LV alebo náhľadu LV, ktorý ste si uložili (HTML alebo PDF); aj celý priečinok naraz; pri bytovom dome načíta aj byty a nebytové priestory s vchodom, poschodím, podielom a vlastníkmi |
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
  Vlastníkov zistí najviac pre 50 parciel denne. Limity platia spoločne pre všetky spustené procesy (viac MCP klientov
  aj príkazový riadok) a nedajú sa vypnúť nastavením ani zmenou dátového adresára.

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

## Príkazový riadok

Balík obsahuje aj príkaz `sk-kataster`. Bez inštalácie ho spustíte cez
`uvx --from git+https://github.com/braklo/kataster-mcp sk-kataster`, výstup je JSON.

```bash
sk-kataster doctor
sk-kataster ku resolve 'Vyšný Kubín'
sk-kataster lv inspect ~/Stiahnuté/vypis.html
sk-kataster lv import ~/Stiahnuté/vypis.html
sk-kataster person overview --surname Novák --first-name Ján
sk-kataster tools
sk-kataster call kataster_parcela '{"ku": "Vyšný Kubín", "cislo": "E 503"}'
```

`doctor` ukáže verziu, adresáre a dnešné využitie kvóty. `ku`, `lv url`, `lv inspect`, `lv import` a
`person overview` na portál nevolajú, `lv inspect` ani `lv import --dry-run` nič neukladajú. Výpis LV vracajú
predvolene v súhrne; parcely a vlastníkov pridá `--full`. `tools` vypíše nástroje servera a `call` jeden z nich
spustí. Ak nástroj ide na portál, platí rovnaká pauza a denný limit ako pre MCP server.

Dátový adresár (cache a uložené LV) presuniete premennou `KATASTER_DATA_DIR`, v príkazovom riadku aj voľbou
`--data-dir`. Stav limitov zostáva na pevnom mieste.

Pre AI klientov, ktorí pracujú cez príkazový riadok, je v repozitári voliteľný návod
[`skills/kataster/SKILL.md`](skills/kataster/SKILL.md).

## Osobné údaje

Mená, dátumy narodenia a adresy vlastníkov sa ukladajú len lokálne, do súboru `osoby.sqlite` v dátovom adresári
servera, a po 7 dňoch sa pri ďalšom dotaze načítajú znova. Dátový adresár (presnú cestu vypíše `kataster_status`):

| Systém | Adresár | Ochrana |
|---|---|---|
| Linux | `~/.local/share/kataster-mcp` (alebo `$XDG_DATA_HOME/kataster-mcp`) | práva 0700 / 0600 |
| macOS | `~/Library/Application Support/kataster-mcp` | práva 0700 / 0600 |
| Windows | `%LOCALAPPDATA%\kataster-mcp` | prístup len pre používateľa (predvolené nastavenie Windows) |

Premenná `KATASTER_DATA_DIR` má prednosť pred týmito umiestneniami na všetkých troch systémoch a platí pre MCP server aj
príkazový riadok.

Denný limit dotazov na vlastníkov, pauza medzi dotazmi a blokácia po CAPTCHA sa evidujú zvlášť, v pevnom adresári, ktorý
nemení žiadna premenná prostredia ani presun dátového adresára (presnú cestu vypíše `kataster_status` ako `limity_dir`):

| Systém | Adresár limitov | Ochrana |
|---|---|---|
| Linux | `~/.local/state/kataster-mcp` | práva 0700 / 0600 |
| macOS | `~/Library/Application Support/kataster-mcp-limits` | práva 0700 / 0600 |
| Windows | `%USERPROFILE%\AppData\Local\kataster-mcp-limits` | prístup len pre používateľa (predvolené nastavenie Windows) |

Server ich neposiela nikam inam než do odpovede vášmu AI asistentovi; ak model beží v cloude, dostane ich aj jeho
poskytovateľ. Za to, ako ich použijete, zodpovedáte vy (GDPR, katastrálny zákon).

## Zdroje dát

Úrad geodézie, kartografie a katastra SR (INSPIRE, ESKN, ZBGIS, výškový model), Výskumný ústav pôdoznalectva a ochrany
pôdy, Národné lesnícke centrum, Ministerstvo pôdohospodárstva a rozvoja vidieka SR, Slovenský vodohospodársky podnik,
Štátna ochrana prírody SR. Podmienky ich použitia platia aj pri použití cez tento server; zdroj uvádzajte.

## Stav

Otestované s Claude Code na Linuxe. Na Windows a macOS prechádzajú automatické testy, v bežnom používaní
to zatiaľ overené nie je; skúsenosti uvítame v issues.

## Chyby a návrhy

[Issues](https://github.com/braklo/kataster-mcp/issues), po slovensky aj po anglicky. Do issue nedávajte mená
vlastníkov ani výpisy z listu vlastníctva.

## Licencia

Kód: [MIT](LICENSE). Licencia kódu sa nevzťahuje na údaje, ktoré server sprostredkúva.
