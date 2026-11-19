"""
Search_Inventory (n8n Code tool): the dealer's live inventory from MarketCheck (by the tenant's
marketcheck_dealer_id), plus a link to the matching search on the dealer's own website, built from the tenant's
URL templates (tenant_inventory_config, gaps filled from inventory_url_platforms).

Same behaviour as the n8n tool: with no condition both new and used are searched at once; F150-style models are
retried as F-150; when MarketCheck finds nothing the dealer's search page is read for a count and prices.
"""
import asyncio
import re
from typing import Any, Optional
from urllib.parse import quote

import httpx

from app.tools.base import NEEDS_DB, NEEDS_MARKETCHECK, NEEDS_TENANT, Tool, ToolContext, ToolFailure, schema

_CONFIG_SQL = """
SELECT id, tenant_id, enabled, marketcheck_dealer_id, inventory_url, inventory_url_new, inventory_url_used,
       inventory_search_url_template, inventory_search_url_template_new, inventory_search_url_template_used,
       slug_word_separator, platform_id
FROM public.tenant_inventory_config WHERE tenant_id = $1 LIMIT 1
"""
_PLATFORM_SQL = """
SELECT url_template_generic, url_template_new, url_template_used FROM public.inventory_url_platforms WHERE id = $1 LIMIT 1
"""
_PATH_CRITICAL = ("make", "make_slug", "make_url", "model", "model_slug", "model_url", "condition")
_BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
}
_TIMEOUT = 8.0


def _enc(value: Any) -> str:
    """JavaScript encodeURIComponent."""
    return quote(str(value or ""), safe="-_.!~*'()")


def slugify(value: Any, word_sep: str = "-") -> str:
    sep = "_" if word_sep == "_" else "-"
    s = str(value or "").strip().lower().replace("&", " and ")
    s = re.sub(r" +", sep, s)
    s = re.sub(r"[^a-z0-9_-]+", "-", s)
    s = re.sub(r"^[-_]+|[-_]+$", "", s)
    return re.sub(r"_{2,}", "_", re.sub(r"-{2,}", "-", s))


def _digits(value: Any) -> str:
    return re.sub(r"[^0-9]", "", str(value or ""))


def _trailing_slash(url: Any) -> str:
    s = str(url or "").strip()
    return s if not s or s.endswith("/") else s + "/"


def _render(template: str, values: dict[str, Any]) -> str:
    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", lambda m: "" if values.get(m.group(1)) is None else str(values[m.group(1)]), template)


def _cleanup(url: str) -> str:
    out = str(url or "").strip()
    if not out:
        return out
    for pattern, repl in (
        (r"/model-/", "/"), (r"/model-$", ""), (r"/trim-/", "/"), (r"/trim-$", ""), (r"/color-/", "/"), (r"/color-$", ""),
        (r"\?years=$", ""), (r"\?years=&", "?"), (r"&years=$", ""), (r"/\?", "?"),
    ):
        out = re.sub(pattern, repl, out)
    return out


def _usable(template: Optional[str], values: dict[str, Any]) -> bool:
    t = str(template or "").strip()
    if not t:
        return False
    for key in _PATH_CRITICAL:
        if re.search(r"\{\{\s*" + key + r"\s*\}\}", t) and not str(values.get(key) or "").strip():
            return False
    return True


def _choose_template(row: dict[str, Any], condition: str, values: dict[str, Any]) -> Optional[str]:
    if condition == "new" and _usable(row.get("inventory_search_url_template_new"), values):
        return row["inventory_search_url_template_new"]
    if condition in ("used", "certified") and _usable(row.get("inventory_search_url_template_used"), values):
        return row["inventory_search_url_template_used"]
    if _usable(row.get("inventory_search_url_template"), values):
        return row["inventory_search_url_template"]
    return None


def _query(params: list[tuple[str, Any]]) -> str:
    parts = [f"{_enc(k)}={_enc(str(v).strip())}" for k, v in params if v is not None and str(v).strip() != ""]
    return "?" + "&".join(parts) if parts else ""


def _fallback_url(base: str, f: dict[str, str], condition: str, sep: str) -> Optional[str]:
    if not base:
        return None
    base = base.rstrip("/")
    parts = [base]
    if condition and not re.search("/" + re.escape(condition) + "$", base, re.I):
        parts.append(condition)
    parts += [slugify(f[k], sep) for k in ("make", "model") if f.get(k)]
    qp = [("years", f.get("year")), ("max_price", _digits(f.get("max_price"))), ("max_miles", _digits(f.get("max_miles"))),
          ("trim", f.get("trim")), ("color", f.get("color"))]
    return _cleanup("/".join(parts) + _query(qp))


def _apollo_url(row: dict[str, Any], f: dict[str, str], condition: str, sep: str) -> Optional[str]:
    if str(row.get("platform_id") or "").strip().lower() != "apollo" or condition not in ("new", "used", "certified"):
        return None
    base = (row.get("inventory_url_new") if condition == "new" else row.get("inventory_url_used")) or row.get("inventory_url")
    if not base:
        return None
    parts = [str(base).strip().rstrip("/")] + [slugify(f[k], sep) for k in ("make", "model") if f.get(k)]
    qp = [("paymenttype", "cash"), ("years", f.get("year")), ("intransit", "true"), ("instock", "true"), ("inproduction", "true")]
    return _cleanup("/".join(parts) + _query(qp))


def customer_inventory_url(row: dict[str, Any], f: dict[str, str], condition: str) -> Optional[str]:
    """The dealer-website search page for these filters."""
    url, url_new, url_used = (_trailing_slash(row.get(k)) for k in ("inventory_url", "inventory_url_new", "inventory_url_used"))
    sep = "_" if row.get("slug_word_separator") == "_" else "-"
    values = {
        "inventory_url": url, "inventory_url_new": url_new or url, "inventory_url_used": url_used or url,
        "make": f.get("make", ""), "make_slug": slugify(f.get("make"), sep), "make_url": _enc(f.get("make")),
        "model": f.get("model", ""), "model_slug": slugify(f.get("model"), sep), "model_url": _enc(f.get("model")),
        "year": f.get("year", ""), "year_url": _enc(f.get("year")),
        "trim": f.get("trim", ""), "trim_slug": slugify(f.get("trim"), sep), "trim_url": _enc(f.get("trim")),
        "color": f.get("color", ""), "color_slug": slugify(f.get("color"), sep), "color_url": _enc(f.get("color")),
        "condition": condition or "", "max_price": _digits(f.get("max_price")), "max_miles": _digits(f.get("max_miles")),
    }
    apollo = _apollo_url(row, f, condition, sep)
    if apollo:
        return apollo
    template = _choose_template(row, condition, values)
    if template:
        return _cleanup(_render(template, values))
    base = (url_new or url) if condition == "new" else (url_used or url) if condition in ("used", "certified") else url
    return _fallback_url(base, f, condition, sep)


def hyphenated_model(model: str) -> Optional[str]:
    """F150 -> F-150, RX350 -> RX-350; None when there is nothing to change."""
    if not model:
        return None
    alt = re.sub(r"^([A-Za-z]+)(\d.*)$", r"\1-\2", model)
    return alt if alt != model else None


def summarize(data: dict[str, Any]) -> dict[str, Any]:
    stats, facets = data.get("stats") or {}, data.get("facets") or {}
    price, miles = stats.get("price"), stats.get("miles")
    facet = lambda name, key: [{key: x.get("item"), "count": x.get("count")} for x in (facets.get(name) or [])]  # noqa: E731
    return {
        "total_count": data.get("num_found") or 0,
        "lowest_price": price.get("min") if price else None,
        "highest_price": price.get("max") if price else None,
        "average_price": round(price["mean"]) if price and price.get("mean") is not None else None,
        "lowest_miles": miles.get("min") if miles else None,
        "average_miles": round(miles["mean"]) if miles and miles.get("mean") is not None else None,
        "models_summary": facet("model", "model"),
        "trims_summary": facet("trim", "trim"),
        "colors_available": facet("base_ext_color", "color"),
        "years_available": facet("year", "year"),
    }


async def _marketcheck(ctx: ToolContext, row: dict[str, Any], f: dict[str, str], car_type: str, model: Optional[str] = None) -> dict[str, Any]:
    params: list[tuple[str, Any]] = [
        ("api_key", ctx.settings.marketcheck_api_key), ("dealer_id", row["marketcheck_dealer_id"]), ("make", f["make"]),
        ("stats", "price,miles"), ("facets", "model|0|50,trim|0|50,base_ext_color|0|20,year|0|10"),
        ("model", f.get("model") if model is None else model), ("year", f.get("year")), ("trim", f.get("trim")),
        ("base_ext_color", f.get("color")),
    ]
    if car_type == "new":
        params.append(("car_type", "new"))
    elif car_type == "used":
        params += [("car_type", "used"), ("exclude_certified", "true")]
    elif car_type == "certified":
        params.append(("car_type", "certified"))
    if _digits(f.get("max_miles")):
        params.append(("miles_range", f"0-{_digits(f.get('max_miles'))}"))
    if _digits(f.get("max_price")):
        params.append(("price_range", f"0-{_digits(f.get('max_price'))}"))
    url = ctx.settings.marketcheck_base_url.rstrip("/") + "/v2/search/car/active" + _query(params)
    try:
        response = await ctx.http.get(url, headers={"Accept": "application/json"}, timeout=_TIMEOUT)
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"error": {"code": None, "message": f"{type(exc).__name__}: {exc}"[:200]}}
    if response.status_code >= 400 or (isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] >= 400):
        message = data.get("message") if isinstance(data, dict) else None
        return {"error": {"code": (data or {}).get("code") or response.status_code, "message": message or "Unknown error"}}
    return {"data": data if isinstance(data, dict) else {}, "condition": car_type}


def _html_text(html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&#160;", " ", text).replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def _number(value: str) -> Optional[int]:
    digits = _digits(value)
    return int(digits) if digits else None


def _apply_prices(out: dict[str, Any], prices: list[int]) -> None:
    clean = [p for p in prices if p is not None and p >= 1000]
    if clean:
        out.update(lowest_price=min(clean), highest_price=max(clean), average_price=round(sum(clean) / len(clean)))


async def website_summary(ctx: ToolContext, url: Optional[str], f: dict[str, str]) -> dict[str, Any]:
    """Count and prices read from the dealer's own search page, when MarketCheck had nothing."""
    out: dict[str, Any] = {"total_count": 0, "lowest_price": None, "highest_price": None, "average_price": None,
                           "models_summary": [], "trims_summary": [], "years_available": [], "inventory_url": url,
                           "source": "dealer_website_fallback"}
    if not url:
        return out
    try:
        response = await ctx.http.get(url, headers=_BROWSER_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        text = _html_text(response.text)
    except httpx.HTTPError as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return out
    make, model, trim, condition = (re.sub(r"\s+", " ", str(f.get(k) or "").strip()) for k in ("make", "model", "trim", "condition"))
    prefix = f"(?:{re.escape(condition)}\\s+)?" if condition else r"(?:New|Used|Certified)?\s*"
    preferred = r"\b(?:Your Price|MSRP|Retail Price|Starting at)\s*\$?\s*([0-9]{1,3}(?:,[0-9]{3})+)"
    years: dict[str, int] = {}
    if make and model and trim:
        cards = list(re.finditer(
            rf"\b{prefix}(20\d{{2}})\s+{re.escape(make)}\s+{re.escape(model)}\s+{re.escape(trim)}\b[\s\S]*?"
            r"(?=\b(?:New|Used|Certified)?\s*20\d{2}\s+[A-Z][A-Za-z-]+\s+|Back to Top|Explore our Other|$)", text, re.I,
        ))
        prices = []
        for card in cards:
            years[card.group(1)] = years.get(card.group(1), 0) + 1
            m = re.search(preferred, card.group(0), re.I) or re.search(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})+)", card.group(0))
            if m:
                prices.append(_number(m.group(1)))
        out["total_count"] = len(cards)
        out["years_available"] = [{"year": y, "count": c} for y, c in years.items()]
        _apply_prices(out, prices)
        return out
    count = re.search(r"\bResults:\s*(\d{1,5})\s+Vehicles?\b", text, re.I) or re.search(r"\b(\d{1,5})\s+vehicles?\s+(?:available|found)\b", text, re.I)
    if count:
        out["total_count"] = int(count.group(1))
    _apply_prices(out, [_number(m.group(1)) for m in re.finditer(preferred, text, re.I)])
    pattern = (rf"\b(20\d{{2}})\s+{re.escape(make)}\s+{re.escape(model)}\b" if make and model else r"\b(20\d{2})\s+[A-Z][A-Za-z-]+\s+[A-Z0-9][A-Za-z0-9 -]+")
    for m in re.finditer(pattern, text, re.I if make and model else 0):
        years[m.group(1)] = years.get(m.group(1), 0) + 1
    if years:
        out["years_available"] = [{"year": y, "count": c} for y, c in years.items()]
    return out


async def _inventory_config(ctx: ToolContext) -> dict[str, Any]:
    pool = ctx.need_pool()
    row = await pool.fetchrow(_CONFIG_SQL, ctx.tenant_id)
    if row is None:
        raise ToolFailure("this dealership has no inventory search configured (tenant_inventory_config)")
    row = dict(row)
    if row.get("enabled") is not True:
        raise ToolFailure("inventory search is turned off for this dealership")
    if row.get("marketcheck_dealer_id") in (None, ""):
        raise ToolFailure("this dealership has no marketcheck_dealer_id")
    if row.get("platform_id"):
        try:  # the tenant's own templates win; the shared platform only fills gaps
            platform = await pool.fetchrow(_PLATFORM_SQL, str(row["platform_id"]).strip())
        except Exception:  # noqa: BLE001
            platform = None
        if platform:
            for own, shared in (("inventory_search_url_template", "url_template_generic"),
                                ("inventory_search_url_template_new", "url_template_new"),
                                ("inventory_search_url_template_used", "url_template_used")):
                if not row.get(own) and platform[shared]:
                    row[own] = platform[shared]
    return row


async def search_inventory(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    f = {k: str(args.get(k) or "").strip() for k in ("make", "model", "year", "trim", "condition", "color", "max_miles", "max_price")}
    if not f["make"]:
        return {"error": "No make specified. Ask the customer which make they are looking for."}
    row = await _inventory_config(ctx)
    condition = f["condition"].lower()
    url_for = lambda filters, cond: customer_inventory_url(row, {**filters, "condition": cond}, cond) or (  # noqa: E731
        row.get("inventory_url_new") if cond == "new" else row.get("inventory_url_used") if cond in ("used", "certified") else None
    ) or row.get("inventory_url")
    no_match = "No matching vehicles found in this dealer's inventory."

    if not condition:
        new, used = await asyncio.gather(_marketcheck(ctx, row, f, "new"), _marketcheck(ctx, row, f, "used"))
        if new.get("error") and used.get("error"):
            return {"error": "MarketCheck API error (both calls failed)", "new_error": new["error"], "used_error": used["error"]}
        new_sum = summarize(new["data"]) if new.get("data") is not None else {"total_count": 0}
        used_sum = summarize(used["data"]) if used.get("data") is not None else {"total_count": 0}
        new_url, used_url = url_for(f, "new"), url_for(f, "used")
        total = (new_sum["total_count"] or 0) + (used_sum["total_count"] or 0)
        if total:
            return {"total_count": total, "new": {**new_sum, "inventory_url": new_url}, "used": {**used_sum, "inventory_url": used_url},
                    "new_inventory_url": new_url, "used_inventory_url": used_url, "website_url": row.get("inventory_url")}
        alt = hyphenated_model(f["model"])
        if alt:
            new2, used2 = await asyncio.gather(_marketcheck(ctx, row, f, "new", alt), _marketcheck(ctx, row, f, "used", alt))
            new_sum2 = summarize(new2["data"]) if new2.get("data") is not None else {"total_count": 0}
            used_sum2 = summarize(used2["data"]) if used2.get("data") is not None else {"total_count": 0}
            alt_total = (new_sum2["total_count"] or 0) + (used_sum2["total_count"] or 0)
            if alt_total:
                af = {**f, "model": alt}
                new_alt, used_alt = url_for(af, "new"), url_for(af, "used")
                return {"total_count": alt_total, "new": {**new_sum2, "inventory_url": new_alt}, "used": {**used_sum2, "inventory_url": used_alt},
                        "new_inventory_url": new_alt, "used_inventory_url": used_alt, "website_url": row.get("inventory_url"),
                        "model_requested": f["model"], "model_used": alt}
        new_web, used_web = await asyncio.gather(
            website_summary(ctx, new_url, {**f, "condition": "new"}), website_summary(ctx, used_url, {**f, "condition": "used"})
        )
        web_total = (new_web["total_count"] or 0) + (used_web["total_count"] or 0)
        if web_total:
            return {"total_count": web_total, "new": {**new_web, "inventory_url": new_url}, "used": {**used_web, "inventory_url": used_url},
                    "new_inventory_url": new_url, "used_inventory_url": used_url, "website_url": row.get("inventory_url"),
                    "source": "dealer_website_fallback"}
        return {"total_count": 0, "new_inventory_url": new_url, "used_inventory_url": used_url,
                "website_url": row.get("inventory_url"), "message": no_match}

    single = await _marketcheck(ctx, row, f, condition)
    if single.get("error"):
        return {"error": "MarketCheck API error", "code": single["error"]["code"], "message": single["error"]["message"]}
    result, url = summarize(single["data"]), url_for(f, condition)
    if result["total_count"]:
        return {**result, "inventory_url": url, "website_url": row.get("inventory_url")}
    alt = hyphenated_model(f["model"])
    if alt:
        second = await _marketcheck(ctx, row, f, condition, alt)
        if not second.get("error"):
            result2 = summarize(second["data"])
            if result2["total_count"]:
                return {**result2, "inventory_url": url_for({**f, "model": alt}, condition), "website_url": row.get("inventory_url"),
                        "model_requested": f["model"], "model_used": alt}
    web = await website_summary(ctx, url, f)
    if web["total_count"]:
        return {**web, "inventory_url": url, "website_url": row.get("inventory_url"), "source": "dealer_website_fallback"}
    return {"total_count": 0, "inventory_url": url, "website_url": row.get("inventory_url"), "message": no_match}


SEARCH_INVENTORY = Tool(
    name="Search_Inventory",
    kind="code",
    description="Search the dealership's current vehicle inventory by make, model, year, trim, color, condition, "
    "price, and mileage. Use when a customer asks about vehicles in stock, available cars, or specific makes/models. "
    "Call this tool IMMEDIATELY when you can identify at least a make — do not ask for confirmation first.",
    parameters=schema({
        "make": {"type": "string", "description": "Vehicle make/manufacturer (e.g. Ford, Toyota, Honda). Required."},
        "model": {"type": "string", "description": "Vehicle model (e.g. Explorer, Camry, Civic). Optional."},
        "year": {"type": "string", "description": "Model year (e.g. 2026). Optional."},
        "trim": {"type": "string", "description": "Trim level (e.g. XLT, Limited, EX-L). Optional."},
        "condition": {"type": "string", "description": "new, used, or certified. Optional — omit to search both."},
        "color": {"type": "string", "description": "Exterior color (e.g. White, Black, Blue). Optional."},
        "max_miles": {"type": "string", "description": "Maximum mileage for used vehicles (e.g. 50000). Optional."},
        "max_price": {"type": "string", "description": "Maximum price in dollars (e.g. 35000). Optional."},
    }, ["make"]),
    run=search_inventory,
    needs=(NEEDS_TENANT, NEEDS_DB, NEEDS_MARKETCHECK),
    # search_inventory.js reads only tool_ctx.tenant_id ("Lookup Lead"); here that is the request's tenant_id.
    inputs=(),
)
