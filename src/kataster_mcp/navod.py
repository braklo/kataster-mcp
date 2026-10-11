"""Step-by-step guides for what the server cannot do itself (CAPTCHA, the official search by person, eID).

The user is sent to the browser only where the server cannot go. A guide is a list of steps in Slovak for
the user, with links only to pages that have a fixed address (result pages carry session tokens).
"""

SEARCH_URL = "https://kataster.skgeodesy.sk/eskn-portal/vyhladavanie"
SLOVENSKO_SK = "https://www.slovensko.sk"

SAVE_PREVIEW = ("Ulož stránku Náhľadu LV: Ctrl+S → typ „Webová stránka, kompletná“ (nie „iba HTML“; adresa v prehliadači "
                "ostáva na predchádzajúcej stránke, uložil by sa nesprávny obsah). Všetky súbory ukladaj do jedného priečinka.")
CAPTCHA = ("Ak portál zobrazí „Nie som robot“ (reCAPTCHA), klikni ho sám; server CAPTCHA nikdy nerieši ani neobchádza.")


def _ku_text(unit: dict | None) -> str:
    return f"{unit['nazov']} ({unit['kod']})" if unit else "katastrálne územie"


def parcel(unit: dict | None, register: str | None, cislo: str | None) -> list[str]:
    """Owners of one parcel by hand: parcel search -> detail -> LV preview -> import."""
    button = (f"„Parcely registra {register}“" if register in ("C", "E")
              else "„Parcely registra C“ alebo „Parcely registra E“ (podľa registra parcely)")
    return [
        f"Otvor {SEARCH_URL} (Vyhľadávanie na portáli ESKN, bez prihlásenia).",
        f"Klikni {button}.",
        f"Do poľa „Obec alebo katastrálne územie“ zadaj {_ku_text(unit)} (alebo ho vyber cez Kraj → Okres → Obec → "
        f"K.ú.), do poľa „Číslo parcely registra {register or 'C/E'}“ číslo {cislo or 'parcely'} a vyhľadaj.",
        "Otvor detail parcely: sú tam vlastníci, podiely a číslo listu vlastníctva (LV).",
        "Klikni na číslo LV: otvorí sa „Náhľad listu vlastníctva“ (všetky parcely a vlastníci LV).",
        CAPTCHA,
        SAVE_PREVIEW,
        "Zavolaj kataster_lv_import s cestou k uloženému súboru alebo k priečinku.",
    ]


def lv(url: str) -> list[str]:
    """LV extract: preview vs. PDF extract vs. official extract."""
    return [
        "Vyber si, čo potrebuješ: (1) Náhľad LV na portáli ESKN: zadarmo, informatívny, bez častí so správcami "
        "a nájomcami. (2) Výpis LV (HTML alebo PDF) z tohto odkazu: zadarmo, informatívny („nepoužiteľný na právne "
        "úkony“), ako jediný verejný zdroj obsahuje ťarchy (časť C). (3) Úradný výpis LV na právne úkony: spoplatnený, "
        f"cez {SLOVENSKO_SK} s eID alebo na okresnom úrade, katastrálnom odbore; ten server nesprostredkúva.",
        f"Pre (2) otvor odkaz v prehliadači: {url}",
        CAPTCHA,
        "Ulož stránku: HTML výpis cez Ctrl+S → „Webová stránka, iba HTML“, PDF výpis ako súbor PDF.",
        "Zavolaj kataster_lv_import s cestou k súboru (alebo k priečinku, ak ich máš viac).",
    ]


def person(unit: dict | None, typ: str, priezvisko: str | None, meno: str | None,
           nazov: str | None, ico: str | None) -> list[str]:
    """The official ESKN search by owner, done by the user; then import of the saved LV previews."""
    if typ == "pravnicka":
        who = " a ".join(x for x in (f"názov {nazov}" if nazov else "", f"IČO {ico}" if ico else "") if x)
        find = [
            "V bloku „Vlastníci a iné oprávnené osoby“ klikni „Právnické osoby“.",
            f"Do poľa „Obec alebo katastrálne územie“ zadaj {_ku_text(unit)}, potom vyplň jedno z polí „Názov“ alebo "
            f"„IČO“{f' ({who})' if who else ''} a vyhľadaj.",
            "Výsledok je zoznam zápisov osoba × list vlastníctva: jedna firma môže mať viac riadkov (jeden na každý LV). "
            "Pri rovnakých názvoch rozhoduje IČO a sídlo.",
        ]
    else:
        who = " ".join(x for x in (priezvisko, meno) if x)
        find = [
            "V bloku „Vlastníci a iné oprávnené osoby“ klikni „Fyzické osoby“.",
            f"Do poľa „Obec alebo katastrálne územie“ zadaj {_ku_text(unit)} (alebo ho vyber cez Kraj → Okres → Obec → "
            f"K.ú.), vyplň priezvisko a meno{f' ({who})' if who else ''} a vyhľadaj.",
            "Výsledok je zoznam zápisov osoba × list vlastníctva (10 na stranu, listuj ďalej). Môžu tam byť menovci: "
            "svoje riadky spoznáš podľa dátumu narodenia a trvalého pobytu.",
        ]
    return [
        f"Otvor {SEARCH_URL} (Vyhľadávanie na portáli ESKN, bez prihlásenia).",
        *find,
        CAPTCHA,
        "Pri každom svojom riadku klikni „Zobraziť“: zobrazí sa číslo LV a poradové číslo osoby na LV.",
        "Klikni na číslo LV: otvorí sa „Náhľad listu vlastníctva“.",
        SAVE_PREVIEW,
        "Vráť sa na zoznam a zopakuj to pre ďalšie riadky (každý LV stačí uložiť raz).",
        "Zavolaj kataster_lv_import s cestou k priečinku a potom kataster_osoba_prehlad.",
    ]
