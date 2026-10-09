"""kataster_skupina: a group of parcels — areas, mutual adjacency, nearest reference, neighbours."""

import warnings
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from shapely.ops import unary_union

from . import geo
from .cache import DAY, ts_iso
from .envelope import Envelope, KatasterError
from .sources import wfs

MAX_PARCELS = 50
TOUCH_M = 0.3      # boundary tolerance for "shares a boundary"
MIN_SHARED_M = 0.5  # below this it is only a corner touch


with warnings.catch_warnings():
    # "register" shadows BaseModel.register (ABC helper); the field name is part of the public API.
    warnings.filterwarnings("ignore", message='Field name "register"')

    class ParcelRef(BaseModel):
        ku: Annotated[str, Field(description="Cadastral unit (k.ú.): name or 6-digit code.")]
        cislo: Annotated[str, Field(description="Parcel number, e.g. '503', 'E 503'.")]
        register: Literal["auto", "C", "E"] = "auto"


def _label(p: dict) -> str:
    return f"{p['ku_kod']} {p['register']} {p['cislo']}"


def shared_boundary_m(a, b, lat0: float) -> float:
    am, bm = geo.to_metric(a, lat0), geo.to_metric(b, lat0)
    return round(bm.buffer(TOUCH_M).intersection(am.boundary).length, 1)


def register(mcp, app) -> None:
    from .server import _wfs_cached, find_parcel

    @mcp.tool()
    async def kataster_skupina(
        parcely: Annotated[list[ParcelRef], Field(description=f"Parcels of the group (max {MAX_PARCELS}).")],
        referencne: Annotated[list[ParcelRef] | None, Field(description="Reference parcels (e.g. own land); for each group parcel the nearest one and the distance.")] = None,
        susedia: Annotated[bool, Field(description="Also list neighbouring parcels from the WFS (same register) with shared boundary length.")] = False,
    ) -> dict:
        """Analyse a group of parcels from official geometry (no owners).

        Returns each parcel's area, the total, which group parcels share a boundary with each
        other (length in m), whether the group is one contiguous block, the nearest reference
        parcel with distance in m, and optionally all neighbouring parcels.
        """
        env = Envelope()
        try:
            refs_in = referencne or []
            if not parcely:
                raise KatasterError("invalid_input", "Give at least one parcel.")
            if len(parcely) + len(refs_in) > MAX_PARCELS:
                raise KatasterError("invalid_input", f"At most {MAX_PARCELS} parcels (group + references) per call.")
            group = [await find_parcel(app, env, r.ku, r.cislo, r.register) for r in parcely]
            refs = [await find_parcel(app, env, r.ku, r.cislo, r.register) for r in refs_in]
            shapes = {_label(p): geo.parcel_shape(p["geometria"]) for p in group + refs}
            lat0 = group[0]["referencny_bod"]["lat"]

            rows = []
            for p in group:
                lab = _label(p)
                s = shapes[lab]
                row = {"parcela": lab, "vymera_m2": p["vymera_m2"], "hranici_s": []}
                for q in group:
                    ql = _label(q)
                    if ql != lab:
                        m = shared_boundary_m(s, shapes[ql], lat0)
                        if m >= MIN_SHARED_M:
                            row["hranici_s"].append({"parcela": ql, "spolocna_hranica_m": m})
                if refs:
                    sm = geo.to_metric(s, lat0)
                    best = min(refs, key=lambda r: sm.distance(geo.to_metric(shapes[_label(r)], lat0)))
                    row["najblizsia_referencna"] = {
                        "parcela": _label(best),
                        "vzdialenost_m": round(sm.distance(geo.to_metric(shapes[_label(best)], lat0)), 1),
                    }
                if susedia:
                    bounds = s.buffer(0.00002).bounds
                    key = f"nb|{p['oznacenie']}"
                    found = await _wfs_cached(app, env, key, p["register"],
                                              lambda b=bounds, reg=p["register"]: wfs.in_bbox(app.http, b, reg))
                    nb = []
                    for q in found:
                        if q["oznacenie"] == p["oznacenie"]:
                            continue
                        m = shared_boundary_m(s, geo.parcel_shape(q["geometria"]), lat0)
                        if m >= MIN_SHARED_M:
                            nb.append({"parcela": _label(q), "vymera_m2": q["vymera_m2"], "spolocna_hranica_m": m})
                    row["susedia"] = sorted(nb, key=lambda x: -x["spolocna_hranica_m"])
                rows.append(row)

            merged = unary_union([geo.to_metric(shapes[_label(p)], lat0).buffer(TOUCH_M) for p in group])
            blocks = len(merged.geoms) if hasattr(merged, "geoms") else 1
            return env.ok({
                "parcely": rows,
                "spolu_vymera_m2": sum(p["vymera_m2"] for p in group),
                "pocet_suvislych_celkov": blocks,
            })
        except KatasterError as e:
            raise env.fail(e) from e
