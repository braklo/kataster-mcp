"""Point/polygon layers about a parcel: soil (BPEJ), official land value, LPIS, land user,
flood maps, nature protection, elevation.

Every function takes a Target (parcel polygon and/or point) and returns plain data.
Shares (podiel) are fractions of the parcel area; m² are share x cadastral area.
All endpoints verified 2026-10-07 with positive controls (a point or parcel where the layer has data).
"""

import json
from dataclasses import dataclass

from shapely.geometry import Point

from .. import geo
from ..envelope import KatasterError
from . import dmr, eskn, kn_wms, pu, zbgis

VUPOP = "https://portal.vupop.sk/arcgis/rest/services"
BPEJ_URL = f"{VUPOP}/BPEJ/BPEJ_parametre_pro/MapServer"
VALUE_URL = f"{VUPOP}/BPEJ/Hodnota_pozemkov_pre_pozemkove_upravy/MapServer"
LPIS_URL = f"{VUPOP}/LPIS/LPIS_aktualny_a_lokality/MapServer"
NLC_URL = "https://gis.nlcsk.org/arcgis/rest/services/MPRV/hranice_uzivania_zbgis/MapServer/0/query"
NLC_JPRL_URL = "https://gis.nlcsk.org/arcgis/rest/services/Inspire/JPRL/MapServer"
NLC_TYPES_URL = "https://gis.nlcsk.org/arcgis/rest/services/Inspire/LesneTypy/MapServer"
SVP_URL = "https://mpt.svp.sk/gisserver/rest/services/OP_MPOMPR_II/MPO/MapServer"
SOP_URL = "https://www.sopsr.sk/geoserver"
DMR_URL = "https://zbgisws.skgeodesy.sk/zbgis_dmr_wms/service.svc/get"

# SVP: flood extent polygon layers per return period, and depth rasters.
SVP_EXTENT = {"3": "Q10", "6": "Q10", "10": "Q100", "13": "Q100", "17": "Q1000", "20": "Q1000"}
SVP_DEPTH = {"24": "Q10", "27": "Q10", "31": "Q100", "34": "Q100", "38": "Q1000", "41": "Q1000"}

SOP_LAYERS = [
    ("chranene_objekty", "chranene_uzemia_chvu", "SHAPE", "CHVÚ (bird protection area, Natura 2000)"),
    ("chranene_objekty", "chranene_uzemia_uev", "SHAPE", "ÚEV (site of Community importance, Natura 2000)"),
    ("chranene_objekty", "chranene_uzemia_vchu", "SHAPE", "VCHÚ (large protected area: national park, CHKO)"),
    ("chranene_objekty", "chranene_uzemia_mchu", "SHAPE", "MCHÚ (small protected area: reserve, monument)"),
    ("sz", "sz_protection_degree", "geom", "degree of protection 2-5"),
]


@dataclass
class Target:
    lat: float
    lon: float
    geom: object | None = None  # shapely, WGS84
    vymera_m2: float | None = None
    register: str | None = None  # C or E for a parcel, None for a point
    cislo: str | None = None
    ku_kod: str | None = None

    @property
    def polygon(self):
        return geo.query_polygon(self.geom) if self.geom is not None else None


def _num(v):
    if v in (None, "", "Null"):
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def _int(v):
    n = _num(v)
    return int(n) if n is not None else None


def _extent(t: Target, pad=0.002) -> str:
    if t.geom is not None:
        minx, miny, maxx, maxy = t.geom.bounds
    else:
        minx = maxx = t.lon
        miny = maxy = t.lat
    return f"{minx - pad},{miny - pad},{maxx + pad},{maxy + pad}"


def _ags_geometry(t: Target) -> tuple[str, str]:
    if t.geom is not None:
        return json.dumps({"rings": geo.esri_rings(t.polygon), "spatialReference": {"wkid": 4326}}), "esriGeometryPolygon"
    return json.dumps({"x": t.lon, "y": t.lat, "spatialReference": {"wkid": 4326}}), "esriGeometryPoint"


def identify_params(t: Target, layers: str, return_geometry: bool, point_only=False) -> dict:
    if point_only:
        g, gt = json.dumps({"x": t.lon, "y": t.lat, "spatialReference": {"wkid": 4326}}), "esriGeometryPoint"
    else:
        g, gt = _ags_geometry(t)
    return {
        "f": "json", "geometry": g, "geometryType": gt, "sr": 4326, "tolerance": 0,
        "mapExtent": _extent(t), "imageDisplay": "1000,1000,96", "layers": layers,
        "returnGeometry": "true" if return_geometry else "false",
    }


def _check_ags(data: dict, what: str) -> list:
    if "error" in data:
        raise KatasterError("unavailable", f"{what}: {data['error'].get('message')}")
    return data["results"]


def _share_and_area(t: Target, rings) -> dict:
    if t.geom is None or not rings:
        return {}
    s = geo.share(geo.esri_rings_to_shape(rings), t.geom)
    out = {"podiel": round(s, 4)}
    if t.vymera_m2:
        out["vymera_m2"] = round(s * t.vymera_m2)
    return out


def _merge_by(rows: list[dict], key: str) -> list[dict]:
    """Several polygons of the same class -> one row with summed share."""
    merged: dict = {}
    for r in rows:
        k = r[key]
        if k in merged:
            for f in ("podiel", "vymera_m2"):
                if f in r:
                    merged[k][f] = round(merged[k].get(f, 0) + r[f], 4 if f == "podiel" else None)
        else:
            merged[k] = dict(r)
    return sorted(merged.values(), key=lambda r: -r.get("podiel", 0))


# --- BPEJ -------------------------------------------------------------------

def parse_bpej(data: dict, t: Target) -> list[dict]:
    results = _check_ags(data, "VÚPOP BPEJ")
    protected = {r["attributes"]["BPEJ"] for r in results if r["layerId"] == 1}
    rows = []
    for r in results:
        if r["layerId"] != 0:
            continue
        a = r["attributes"]
        rows.append({
            "bpej": a["BPEJ"],
            "klimaticky_region": a.get("Klimatické regióny"),
            "podny_typ": a.get("Pôdne typy"),
            "skelet": a.get("Obsah skeletu"),
            "hlbka": a.get("Hĺbka pôdy"),
            "zrnitost": a.get("Zrnitosť pôdy"),
            "bodova_hodnota": _int(a.get("Bodová hodnota produkčného potenciálu")),
            "typologicko_produkcna_kategoria": a.get("Typologicko-produkčná kategória"),
            "skupina_kvality": _int(a.get("Skupina kvality")),
            "odvod_trvaly_zaber_eur_m2": _num(a.get("Trvalý záber [€/m²]")),
            "odvod_docasny_zaber_eur_m2": _num(next((v for k, v in a.items() if k.startswith("Dočasný záber")), None)),
            "chranena_poda": a["BPEJ"] in protected,
            **_share_and_area(t, (r.get("geometry") or {}).get("rings")),
        })
    return _merge_by(rows, "bpej")


async def bpej(http, t: Target) -> list[dict]:
    data = await http.get_json(f"{BPEJ_URL}/identify", identify_params(t, "all:0,1", t.geom is not None))
    return parse_bpej(data, t)


# --- Official land value (vyhl. 38/2005, for land consolidation) -------------

def parse_value(data: dict, t: Target) -> dict:
    results = _check_ags(data, "VÚPOP land value")
    rows = []
    for r in results:
        a = r["attributes"]
        rows.append({"eur_m2": _num(a.get("EUR_4") or a.get("EUR")),
                     **_share_and_area(t, (r.get("geometry") or {}).get("rings"))})
    rows = _merge_by(rows, "eur_m2")
    out = {"pasma": rows, "poznamka": "official value for land consolidation (vyhláška 38/2005), not a market price"}
    if t.vymera_m2 and rows and all("vymera_m2" in r for r in rows):
        out["hodnota_pozemku_eur"] = round(sum(r["eur_m2"] * r["vymera_m2"] for r in rows if r["eur_m2"]), 2)
    return out


async def value(http, t: Target) -> dict:
    data = await http.get_json(f"{VALUE_URL}/identify", identify_params(t, "all", t.geom is not None))
    return parse_value(data, t)


# --- LPIS ---------------------------------------------------------------------

def parse_lpis(data: dict, t: Target) -> list[dict]:
    results = _check_ags(data, "VÚPOP LPIS")
    rows = []
    for r in results:
        if r["layerId"] != 0:
            continue
        a = r["attributes"]
        rows.append({
            "kod_dpb": a.get("KOD_DPB"),
            "zkod_dpb": a.get("ZKOD_DPB"),
            "lokalita": a.get("LOKALITA_D"),
            "kultura": a.get("Kultúra – názov"),
            "kultura_skratka": a.get("Kultúra – skratka"),
            "vymera_dpb_ha": _num(a.get("VYMERA_DPB")),
            **_share_and_area(t, (r.get("geometry") or {}).get("rings")),
        })
    return _merge_by(rows, "kod_dpb")


async def lpis(http, t: Target) -> list[dict]:
    data = await http.get_json(f"{LPIS_URL}/identify", identify_params(t, "all:0", t.geom is not None))
    return parse_lpis(data, t)


# --- Land user (NLC, from subsidy applications) -------------------------------

def nlc_params(t: Target) -> dict:
    g, gt = _ags_geometry(t)
    return {"f": "json", "geometry": g, "geometryType": gt, "inSR": 4326, "outSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outFields": "KODKD,NAZOV_SUBJEKTU,ICO,Rok_Ziadosti",
            "returnGeometry": "true" if t.geom is not None else "false"}


def parse_user(data: dict, t: Target) -> dict:
    if "error" in data:
        raise KatasterError("unavailable", f"NLC land user: {data['error'].get('message')}")
    rows = []
    for f in data["features"]:
        a = f["attributes"]
        rows.append({
            "nazov": a.get("NAZOV_SUBJEKTU"),
            "ico": a.get("ICO"),
            "kulturny_diel": a.get("KODKD"),
            "rok_ziadosti": a.get("Rok_Ziadosti"),
            **_share_and_area(t, (f.get("geometry") or {}).get("rings")),
        })
    return {
        "uzivatelia": sorted(rows, key=lambda r: -r.get("podiel", 0)),
        "poznamka": "From farm subsidy applications (latest year in the service); land nobody applied for has no user here.",
    }


async def user(http, t: Target) -> dict:
    return parse_user(await http.get_json(NLC_URL, nlc_params(t)), t)


# --- Forest (NLC: forest stands JPRL and forest types) --------------------------
# Open data of NLC (IČO 42001315): "JPRL - priestorové údaje" CC BY 4.0, "Lesné typy" CC0 (NKOD, verified 2026-10-08).
# No manager or owner in either service; free-text fields (POZN, text) are left out on purpose.

FOREST_CATEGORY = {"H": "hospodársky", "O": "ochranný", "U": "osobitného určenia"}


def _txt(v):
    v = " ".join(v.split()) if isinstance(v, str) else v
    return None if v in ("", "Null", None) else v


def _codelist(v):
    """'https://registry.nlcsk.org/codelist/ForestVegetationLevelValue/jedlovoBukovy' -> 'jedlovoBukovy'."""
    v = _txt(v)
    return v.rsplit("/", 1)[-1] if v else None


def parse_forest(jprl: dict, types: dict, t: Target, year: int) -> dict:
    stands = []
    for r in _check_ags(jprl, "NLC JPRL"):
        a = r["attributes"]
        kl = _txt(a.get("KL"))
        stands.append({
            "jprl": f"{a.get('DC')}{_txt(a.get('CP')) or ''}{a.get('PS') if a.get('PS') is not None else ''}",
            "dielec": _int(a.get("DC")), "ciastkova_plocha": _txt(a.get("CP")), "porastova_skupina": _int(a.get("PS")),
            "druh": _txt(a.get("TXDP_SKR")), "druh_pozemku": _txt(a.get("DRUH_POZ")),
            "kategoria_lesa": {"kod": kl, "nazov": FOREST_CATEGORY.get(kl)} if kl else None,
            "lesny_celok": _txt(a.get("TxLC")), "kod_lesneho_celku": _txt(a.get("KPL")), "lhc": _txt(a.get("LHC")),
            "lhp_platnost": {"od": _int(a.get("RZP")), "do": _int(a.get("RUP"))},
            "vymera_jprl_m2": _num(a.get("Plocha")),
            **_share_and_area(t, (r.get("geometry") or {}).get("rings")),
        })
    forest_types = []
    for r in _check_ags(types, "NLC forest types"):
        a = r["attributes"]
        # identify returns field aliases as keys
        forest_types.append({
            "lesny_typ": _txt(a.get("Nazov lesneho typu 1")), "kod_lesneho_typu": _txt(a.get("Kod lesneho typu 1")),
            "hslt": _txt(a.get("Nazov HSLT")), "kod_hslt": _txt(a.get("Kod HSLT")),
            "vegetacny_stupen": _codelist(a.get("Lesny vegetacny stupen")),
            "kod_vegetacneho_stupna": _int(a.get("Kod Lesneho vegetacneho stupna")),
            **_share_and_area(t, (r.get("geometry") or {}).get("rings")),
        })
    stands = sorted(stands, key=lambda r: -r.get("podiel", 0))
    ended = sorted({s["lhp_platnost"]["do"] for s in stands if s["lhp_platnost"]["do"] and s["lhp_platnost"]["do"] < year})
    out = {
        "lesny_pozemok": bool(stands),
        **({"podiel_lesa": round(min(1.0, sum(s["podiel"] for s in stands)), 4)}
           if stands and all("podiel" in s for s in stands) else {}),
        "jprl": stands,
        "lesne_typy": _merge_by([f for f in forest_types if f["lesny_typ"]], "lesny_typ") if forest_types else [],
        "poznamka": "Forest management units (JPRL) and forest types of NLC. Tree species, age and growing stock "
                    "are not published in these services; no forest manager or owner is returned.",
    }
    if ended:
        out["varovanie"] = (f"les: the forest management plan (LHP) in the data ended in {ended[-1]}; "
                            "a newer plan may exist but is not published here yet.")
    return out


async def forest(http, t: Target) -> dict:
    from datetime import date
    jprl = await http.get_json(f"{NLC_JPRL_URL}/identify", identify_params(t, "all:0", t.geom is not None))
    types = await http.get_json(f"{NLC_TYPES_URL}/identify", identify_params(t, "all:0", t.geom is not None))
    return parse_forest(jprl, types, t, date.today().year)


# --- Floods (SVP, flood hazard maps) ------------------------------------------

def parse_floods(extent: dict, depth: dict) -> dict:
    hits = sorted({SVP_EXTENT[str(r["layerId"])] for r in _check_ags(extent, "SVP flood extent")
                   if str(r["layerId"]) in SVP_EXTENT}, key=lambda q: int(q[1:]))
    depths = {}
    for r in _check_ags(depth, "SVP flood depth"):
        q = SVP_DEPTH.get(str(r["layerId"]))
        h = r["attributes"].get("Raster.hlbka")
        if q and h:
            depths[q] = h
    return {
        "zaplavove_uzemie": hits,
        "hlbka_v_referencnom_bode": depths,
        "poznamka": "Flood hazard maps exist only for river sections with significant flood risk; "
                    "an empty result means outside the mapped flood extent or not mapped at all.",
    }


async def floods(http, t: Target) -> dict:
    extent = await http.get_json(f"{SVP_URL}/identify", identify_params(t, "all:" + ",".join(SVP_EXTENT), False))
    depth = await http.get_json(f"{SVP_URL}/identify",
                                identify_params(t, "all:" + ",".join(SVP_DEPTH), False, point_only=True))
    return parse_floods(extent, depth)


# --- Nature protection (ŠOP SR) -------------------------------------------------

SOP_FIELDS = {
    "SHAPE": "NATIONALSITECODE,SITETITLE_SK,categoryName,AREA_HA",
    "geom": "registry_number,site_name,stupen",
}


def sop_params(ws: str, layer: str, geom_field: str, t: Target) -> dict:
    g = t.polygon if t.geom is not None else Point(t.lon, t.lat)
    return {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": f"{ws}:{layer}",
            "outputFormat": "application/json", "count": 20, "propertyName": SOP_FIELDS[geom_field],
            "CQL_FILTER": f"INTERSECTS({geom_field},{geo.ewkt(g)})"}


def parse_sop(responses: list[tuple[str, str, dict]]) -> dict:
    areas, degrees = [], []
    for layer, label, data in responses:
        for f in data["features"]:
            p = f["properties"]
            if layer == "sz_protection_degree":
                degrees.append({"stupen": p.get("stupen"), "nazov": p.get("site_name"), "kod": p.get("registry_number")})
            else:
                areas.append({"typ": label, "kod": p.get("NATIONALSITECODE"), "nazov": p.get("SITETITLE_SK"),
                              "kategoria": p.get("categoryName"), "rozloha_ha": p.get("AREA_HA")})
    return {
        "chranene_uzemia": areas,
        "stupen_ochrany": degrees or "no area with degree 2-5 found (degree 1 = general protection)",
    }


async def protection(http, t: Target) -> dict:
    responses = []
    for ws, layer, gf, label in SOP_LAYERS:
        data = await http.get_json(f"{SOP_URL}/{ws}/ows", sop_params(ws, layer, gf, t))
        responses.append((layer, label, data))
    try:
        return parse_sop(responses)
    except (KeyError, TypeError) as e:
        raise KatasterError("parser_drift", f"ŠOP SR WFS response changed: {e}") from e


# --- Elevation (DMR 5.0) --------------------------------------------------------

def dmr_params(lat: float, lon: float) -> dict:
    d = 0.0005
    return {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetFeatureInfo", "LAYERS": "1", "QUERY_LAYERS": "1",
            "CRS": "EPSG:4326", "BBOX": f"{lat - d},{lon - d},{lat + d},{lon + d}", "WIDTH": 101, "HEIGHT": 101,
            "I": 50, "J": 50, "INFO_FORMAT": "text/plain", "STYLES": "", "FORMAT": "image/png"}


def parse_dmr(text: str) -> float | None:
    parts = [p.strip() for p in text.split(";")]
    if len(parts) < 2 or "Pixel Value" not in parts[0]:
        raise ValueError(f"unexpected DMR answer: {text[:80]}")
    return round(float(parts[1]), 1) if parts[1] not in ("", "NoData") else None


async def elevation(http, t: Target) -> dict:
    resp = await http.get(DMR_URL, dmr_params(t.lat, t.lon))
    try:
        h = parse_dmr(resp.text)
    except ValueError as e:
        raise KatasterError("parser_drift", str(e)) from e
    return {"nadmorska_vyska_m": h, "bod": {"lat": t.lat, "lon": t.lon}, "model": "DMR 5.0"}


@dataclass(frozen=True)
class Layer:
    fetch: object
    parsers: tuple  # functions whose source hashes into the cache version
    source_id: str
    url: str
    personal: bool  # personal data -> osoby.sqlite
    ttl_days: int
    oficialny: bool = True
    source_for: object = None  # optional Target -> (source id, url, oficialny), when it depends on the target
    parcel_only: bool = False  # skipped for point queries unless asked for
    needs_store: bool = False  # fetch(http, t, store=cache) caches its own downloads
    warn: object = None  # optional data -> warning text or None, added to the envelope (also from cache)

    def source(self, t: Target) -> tuple[str, str, bool]:
        return self.source_for(t) if self.source_for else (self.source_id, self.url, self.oficialny)


def _zu_source(t: Target) -> tuple[str, str, bool]:
    if t.register == "E":
        return eskn.SOURCE_IDENTIFY, eskn.IDENTIFY_URL, False
    # kn_wms_norm is published by ÚGKK SR (GKÚ list of WMS services, CC BY 4.0; verified 2026-10-07).
    return kn_wms.SOURCE, kn_wms.URL, True


LAYERS = {
    "bpej": Layer(bpej, (parse_bpej, _merge_by, _share_and_area), "vupop_bpej", BPEJ_URL, False, 90),
    "hodnota": Layer(value, (parse_value, _merge_by, _share_and_area), "vupop_hodnota", VALUE_URL, False, 90),
    "lpis": Layer(lpis, (parse_lpis, _merge_by, _share_and_area), "vupop_lpis", LPIS_URL, False, 30),
    "uzivatel": Layer(user, (parse_user, nlc_params, _share_and_area), "nlc_uzivatel", NLC_URL, True, 30),
    "zaplavy": Layer(floods, (parse_floods,), "svp_mpo", SVP_URL, False, 180),
    "ochrana": Layer(protection, (parse_sop, sop_params), "sopsr_wfs", SOP_URL, False, 180),
    "vyska": Layer(elevation, (parse_dmr, dmr_params), "zbgis_dmr", DMR_URL, False, 365),
    "zastavane_uzemie": Layer(kn_wms.zastavane_uzemie,
                              (kn_wms, eskn.parse_identify),  # whole module: constants count too
                              kn_wms.SOURCE, kn_wms.URL, False, 90, oficialny=False, source_for=_zu_source),
    "tarchy": Layer(kn_wms.tarchy,
                    (kn_wms,),
                    "kn_wms_tarchy", kn_wms.URL, False, 30, oficialny=False, parcel_only=True,
                    warn=lambda d: kn_wms.WARN_BOUNDARY if d.get("zakreslena_tarcha") else None),
    "pozemkove_upravy": Layer(pu.pozemkove_upravy, (pu,), pu.SOURCE, pu.URL, False, pu.TTL_DAYS,
                              parcel_only=True, needs_store=True, warn=lambda d: pu.WARNING),
    "les": Layer(forest, (parse_forest, _share_and_area, _merge_by, _txt, _codelist, identify_params), "nlc_les", NLC_JPRL_URL,
                 False, 30, warn=lambda d: d.get("varovanie")),
    "siete": Layer(zbgis.siete, (zbgis,), zbgis.SOURCE, zbgis.URL, False, 90, parcel_only=True, warn=zbgis.warnings),
    "sklon": Layer(dmr.sklon, (dmr, elevation, parse_dmr, dmr_params), dmr.SOURCE, dmr.URL, False, 365,
                   parcel_only=True, warn=lambda d: d.get("varovanie")),
}
