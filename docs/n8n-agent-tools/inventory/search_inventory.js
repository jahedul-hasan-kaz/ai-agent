/**
 * Search_Inventory — n8n AI tool
 * MarketCheck dealer analytics + defensive per-tenant deep-link templates
 *
 * // PATCH: dual-URL search (new + used parallel)
 * When the customer omits new/used, fan out to BOTH car_types in parallel
 * and return per-condition counts/prices/URLs.
 */

const make = String(query.make || '').trim();
const model = String(query.model || '').trim();
const year = String(query.year || '').trim();
const trim = String(query.trim || '').trim();
const condition = String(query.condition || '').trim();
const color = String(query.color || '').trim();
const maxMiles = String(query.max_miles || '').trim();
const maxPrice = String(query.max_price || '').trim();

// tool_ctx (n8n#20752 fix): read tenant_id from the Tool Context bundle
// instead of $('Lookup Lead'), eliminating the AI-Agent-tool IPC hang.
const _ctx = ($input.item && $input.item.json && $input.item.json.tool_ctx) || {};
const leadNodeData = { tenant_id: _ctx.tenant_id, id: _ctx.lead_id };
const tenant_id = leadNodeData.tenant_id;

if (!make) {
  return 'Error: No make specified. Ask the customer which make they are looking for.';
}

if (!tenant_id) {
  return JSON.stringify({
    error: 'Missing tenant_id from Lookup Lead',
    lookup_lead_output: leadNodeData
  });
}

const supabaseUrl = $vars.SUPABASE_URL;
const supabaseKey = $vars.SUPABASE_SERVICE_ROLE_KEY;
const mcApiKey = $vars.MARKETCHECK_API_KEY;

if (!supabaseUrl || !supabaseKey) {
  return JSON.stringify({
    error: 'Missing Supabase vars',
    hasSupabaseUrl: !!supabaseUrl,
    hasSupabaseKey: !!supabaseKey
  });
}

if (!mcApiKey) {
  return JSON.stringify({
    error: 'Missing MARKETCHECK_API_KEY'
  });
}

let row = null;

function addParam(parts, key, value) {
  if (value === null || value === undefined) return;
  const str = String(value).trim();
  if (str === '') return;
  parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(str)}`);
}

function slugify(value, wordSep) {
  // PATCH: slug_word_separator (per-tenant space separator)
  // Some dealer sites (e.g. koonsmotors.com) use '_' between words inside
  // a model slug (Mustang Mach-E -> mustang_mach-e). Other punctuation in
  // the original value still collapses to '-'.
  const sep = wordSep === '_' ? '_' : '-';
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/&/g, ' and ')
    .replace(/ +/g, sep)              // spaces -> configured separator
    .replace(/[^a-z0-9_-]+/g, '-')    // other non-alphanumerics -> '-'
    .replace(/^[-_]+|[-_]+$/g, '')    // trim leading/trailing separators
    .replace(/-{2,}/g, '-')
    .replace(/_{2,}/g, '_');
}

function cleanNumberString(value) {
  return String(value || '').replace(/[^0-9]/g, '');
}

function ensureTrailingSlash(url) {
  const str = String(url || '').trim();
  if (!str) return '';
  return str.endsWith('/') ? str : `${str}/`;
}

function renderTemplate(template, values) {
  return String(template).replace(/\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g, (_, key) => {
    const raw = values[key];
    if (raw === null || raw === undefined) return '';
    return String(raw);
  });
}

function cleanupRenderedUrl(url) {
  let out = String(url || '').trim();
  if (!out) return out;

  out = out.replace(/\/model-\//g, '/');
  out = out.replace(/\/model-$/g, '');
  out = out.replace(/\/trim-\//g, '/');
  out = out.replace(/\/trim-$/g, '');
  out = out.replace(/\/color-\//g, '/');
  out = out.replace(/\/color-$/g, '');
  out = out.replace(/\?years=$/, '');
  out = out.replace(/\?years=&/, '?');
  out = out.replace(/&years=$/, '');
  out = out.replace(/\/\?/g, '?');

  return out;
}

function templateNeedsValue(template, placeholderName) {
  const re = new RegExp(`\\{\\{\\s*${placeholderName}\\s*\\}\\}`);
  return re.test(String(template || ''));
}

const PATH_CRITICAL_PLACEHOLDERS = ['make', 'make_slug', 'make_url', 'model', 'model_slug', 'model_url', 'condition'];

function templateIsUsable(template, values) {
  const t = String(template || '').trim();
  if (!t) return false;

  for (const key of PATH_CRITICAL_PLACEHOLDERS) {
    if (templateNeedsValue(t, key)) {
      const value = values[key];
      if (value === null || value === undefined || String(value).trim() === '') {
        return false;
      }
    }
  }

  return true;
}

function chooseTemplate(configRow, effectiveCondition, values) {
  const newTemplate = configRow.inventory_search_url_template_new || '';
  const usedTemplate = configRow.inventory_search_url_template_used || '';
  const genericTemplate = configRow.inventory_search_url_template || '';

  if (effectiveCondition === 'new' && templateIsUsable(newTemplate, values)) {
    return newTemplate;
  }

  if (
    (effectiveCondition === 'used' || effectiveCondition === 'certified') &&
    templateIsUsable(usedTemplate, values)
  ) {
    return usedTemplate;
  }

  if (!effectiveCondition && templateIsUsable(genericTemplate, values)) {
    return genericTemplate;
  }

  if (templateIsUsable(genericTemplate, values)) {
    return genericTemplate;
  }

  return null;
}

function buildFallbackUrl(base, filters, effectiveCondition, wordSep) {
  if (!base) return null;

  const baseWithoutSlash = base.replace(/\/$/, '');
  const parts = [baseWithoutSlash];

  const conditionSegment = String(effectiveCondition || '').trim().toLowerCase();
  if (conditionSegment) {
    // condition_segment_dedupe: condition-specific bases like /inventory/used
    // should not become /inventory/used/used when we append make/model slugs.
    const escaped = conditionSegment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const alreadyInBase = new RegExp(`/${escaped}$`, 'i').test(baseWithoutSlash);
    if (!alreadyInBase) parts.push(conditionSegment);
  }
  if (filters.make)  parts.push(slugify(filters.make,  wordSep));
  if (filters.model) parts.push(slugify(filters.model, wordSep));

  const qp = [];
  if (filters.year) addParam(qp, 'years', filters.year);
  const price = cleanNumberString(filters.maxPrice || '');
  const miles = cleanNumberString(filters.maxMiles || '');
  if (price) addParam(qp, 'max_price', price);
  if (miles) addParam(qp, 'max_miles', miles);
  if (filters.trim)  addParam(qp, 'trim', filters.trim);
  if (filters.color) addParam(qp, 'color', filters.color);

  const url = parts.join('/') + (qp.length ? '?' + qp.join('&') : '');
  return cleanupRenderedUrl(url);
}

function buildApolloInventoryUrl(configRow, filters, effectiveCondition, wordSep) {
  if (String(configRow.platform_id || '').trim().toLowerCase() !== 'apollo') return null;

  const cond = String(effectiveCondition || filters.condition || '').trim().toLowerCase();
  if (cond !== 'new' && cond !== 'used' && cond !== 'certified') return null;

  const base =
    cond === 'new'
      ? (configRow.inventory_url_new || configRow.inventory_url)
      : (configRow.inventory_url_used || configRow.inventory_url);
  if (!base) return null;

  // apollo_inventory_url_normalization: Apollo dealer pages live under
  // /inventory/new|used/<make>/<model>. Shared platform templates currently
  // render /new/<make>/<model> from the site root for Sewell, which is not the
  // customer-facing inventory route.
  const parts = [String(base).trim().replace(/\/$/, '')];
  if (filters.make) parts.push(slugify(filters.make, wordSep));
  if (filters.model) parts.push(slugify(filters.model, wordSep));

  const qp = [];
  addParam(qp, 'paymenttype', 'cash');
  if (filters.year) addParam(qp, 'years', filters.year);
  addParam(qp, 'intransit', 'true');
  addParam(qp, 'instock', 'true');
  addParam(qp, 'inproduction', 'true');

  return cleanupRenderedUrl(parts.join('/') + (qp.length ? '?' + qp.join('&') : ''));
}

function buildCustomerInventoryUrl(configRow, filters, effectiveCondition) {
  const inventoryUrl = ensureTrailingSlash(configRow.inventory_url || '');
  const inventoryUrlNew = ensureTrailingSlash(configRow.inventory_url_new || '');
  const inventoryUrlUsed = ensureTrailingSlash(configRow.inventory_url_used || '');

  const wordSep = configRow.slug_word_separator === '_' ? '_' : '-';
  // PATCH: platform-template resolution + _url placeholders
  // {{*_url}} placeholders are encodeURIComponent-applied raw values, for
  // dealer-website platforms (e.g. dealer.com) that consume URL-encoded query
  // params with original case (Ford -> Ford, Bronco Sport -> Bronco%20Sport).
  // {{*_slug}} placeholders are lowercase + word-separator-joined slugs, for
  // platforms (e.g. koonsmotors.com custom) that consume path slugs.
  const enc = (v) => encodeURIComponent(String(v || ''));
  const values = {
    inventory_url: inventoryUrl,
    inventory_url_new: inventoryUrlNew || inventoryUrl,
    inventory_url_used: inventoryUrlUsed || inventoryUrl,

    make: filters.make || '',
    make_slug: slugify(filters.make || '', wordSep),
    make_url: enc(filters.make),
    model: filters.model || '',
    model_slug: slugify(filters.model || '', wordSep),
    model_url: enc(filters.model),
    year: filters.year || '',
    year_url: enc(filters.year),
    trim: filters.trim || '',
    trim_slug: slugify(filters.trim || '', wordSep),
    trim_url: enc(filters.trim),
    color: filters.color || '',
    color_slug: slugify(filters.color || '', wordSep),
    color_url: enc(filters.color),
    condition: effectiveCondition || '',
    max_price: cleanNumberString(filters.maxPrice || ''),
    max_miles: cleanNumberString(filters.maxMiles || '')
  };

  const apolloUrl = buildApolloInventoryUrl(configRow, filters, effectiveCondition, wordSep);
  if (apolloUrl) {
    return apolloUrl;
  }

  const template = chooseTemplate(configRow, effectiveCondition, values);

  if (template) {
    return cleanupRenderedUrl(renderTemplate(template, values));
  }

  const base =
    effectiveCondition === 'new'
      ? (inventoryUrlNew || inventoryUrl)
      : effectiveCondition === 'used' || effectiveCondition === 'certified'
        ? (inventoryUrlUsed || inventoryUrl)
        : inventoryUrl;

  return buildFallbackUrl(base, filters, effectiveCondition, wordSep);
}

// Run one MarketCheck search for a given car_type ('', 'new', 'used', 'certified').
// Returns { effCond, data } on success or { error: {...} } on HTTP error code.
async function fetchMarketcheck(carType, modelOverride) {
  const qp = [];
  let effCond = '';

  addParam(qp, 'api_key',   mcApiKey);
  addParam(qp, 'dealer_id', row.marketcheck_dealer_id);
  addParam(qp, 'make',      make);
  addParam(qp, 'stats',     'price,miles');
  addParam(qp, 'facets',    'model|0|50,trim|0|50,base_ext_color|0|20,year|0|10');
  addParam(qp, 'model',     modelOverride !== undefined ? modelOverride : model);
  addParam(qp, 'year',      year);
  addParam(qp, 'trim',      trim);
  addParam(qp, 'base_ext_color', color);

  if (carType === 'new') {
    addParam(qp, 'car_type', 'new');
    effCond = 'new';
  } else if (carType === 'used') {
    addParam(qp, 'car_type', 'used');
    addParam(qp, 'exclude_certified', 'true');
    effCond = 'used';
  } else if (carType === 'certified') {
    addParam(qp, 'car_type', 'certified');
    effCond = 'certified';
  }

  if (maxMiles) {
    const m = cleanNumberString(maxMiles);
    if (m) addParam(qp, 'miles_range', `0-${m}`);
  }
  if (maxPrice) {
    const p = cleanNumberString(maxPrice);
    if (p) addParam(qp, 'price_range', `0-${p}`);
  }

  const url = `https://api.marketcheck.com/v2/search/car/active?${qp.join('&')}`;
  const data = await this.helpers.httpRequest({
    method: 'GET',
    url,
    headers: { Accept: 'application/json' },
    json: true,
    timeout: 8000
  });

  if (data?.code && data.code >= 400) {
    return {
      error: {
        code: data.code,
        message: data.message || 'Unknown error',
        request_url: url
      }
    };
  }

  return { effCond, data };
}

function summarize(data) {
  const ps = data?.stats?.price || null;
  const ms = data?.stats?.miles || null;
  const mf = Array.isArray(data?.facets?.model) ? data.facets.model : [];
  const tf = Array.isArray(data?.facets?.trim)  ? data.facets.trim  : [];
  const cf = Array.isArray(data?.facets?.base_ext_color) ? data.facets.base_ext_color : [];
  const yf = Array.isArray(data?.facets?.year)  ? data.facets.year  : [];
  return {
    total_count:      data?.num_found || 0,
    lowest_price:     ps ? ps.min : null,
    highest_price:    ps ? ps.max : null,
    average_price:    ps ? Math.round(ps.mean) : null,
    lowest_miles:     ms ? ms.min : null,
    average_miles:    ms ? Math.round(ms.mean) : null,
    models_summary:   mf.map(f => ({ model: f.item, count: f.count })),
    trims_summary:    tf.map(f => ({ trim:  f.item, count: f.count })),
    colors_available: cf.map(f => ({ color: f.item, count: f.count })),
    years_available:  yf.map(f => ({ year:  f.item, count: f.count }))
  };
}

async function fetchWebsiteInventorySummary(url, helpers, filters = {}) {
  const out = {
    total_count: 0,
    lowest_price: null,
    highest_price: null,
    average_price: null,
    models_summary: [],
    trims_summary: [],
    years_available: [],
    inventory_url: url || null,
    source: 'dealer_website_fallback'
  };
  if (!url) return out;

  const escapeRegex = (value) => String(value || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const compactSpaces = (value) => String(value || '').trim().replace(/\s+/g, ' ');
  const toNumber = (value) => {
    const n = Number(String(value || '').replace(/[^0-9]/g, ''));
    return Number.isFinite(n) ? n : null;
  };

  function applyPrices(prices) {
    const clean = prices.filter(n => Number.isFinite(n) && n >= 1000);
    if (!clean.length) return;
    out.lowest_price = Math.min(...clean);
    out.highest_price = Math.max(...clean);
    out.average_price = Math.round(clean.reduce((a, b) => a + b, 0) / clean.length);
  }

  function extractCardPrice(segment) {
    const preferred = segment.match(/\b(?:Your Price|MSRP|Retail Price|Starting at)\s*\$?\s*([0-9]{1,3}(?:,[0-9]{3})+)/i);
    if (preferred) return toNumber(preferred[1]);
    const first = segment.match(/\$\s*([0-9]{1,3}(?:,[0-9]{3})+)/);
    return first ? toNumber(first[1]) : null;
  }

  try {
    const html = await helpers.httpRequest({
      method: 'GET',
      url,
      headers: {
        Accept: 'text/html,application/xhtml+xml',
        'Accept-Language': 'en-US,en;q=0.9',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36'
      },
      json: false,
      timeout: 8000
    });
    const text = String(html || '')
      .replace(/<script[\s\S]*?<\/script>/gi, ' ')
      .replace(/<style[\s\S]*?<\/style>/gi, ' ')
      .replace(/<[^>]+>/g, ' ')
      .replace(/&nbsp;|&#160;/g, ' ')
      .replace(/&amp;/g, '&')
      .replace(/\s+/g, ' ')
      .trim();

    const make = compactSpaces(filters.make);
    const model = compactSpaces(filters.model);
    const trim = compactSpaces(filters.trim);
    const condition = compactSpaces(filters.condition);
    const conditionPrefix = condition ? `(?:${escapeRegex(condition)}\\s+)?` : '(?:New|Used|Certified)?\\s*';

    if (make && model && trim) {
      // trim_specific_card_count: for trim searches, count visible matching
      // vehicle cards instead of using the broader page-level model count.
      const cardRe = new RegExp(
        `\\b${conditionPrefix}(20\\d{2})\\s+${escapeRegex(make)}\\s+${escapeRegex(model)}\\s+${escapeRegex(trim)}\\b[\\s\\S]*?(?=\\b(?:New|Used|Certified)?\\s*20\\d{2}\\s+[A-Z][A-Za-z-]+\\s+|Back to Top|Explore our Other|$)`,
        'gi'
      );
      const cards = [...text.matchAll(cardRe)];
      if (cards.length > 0) {
        out.total_count = cards.length;
        const yearCounts = new Map();
        const prices = [];
        for (const match of cards) {
          const segment = match[0] || '';
          const year = match[1];
          if (year) yearCounts.set(year, (yearCounts.get(year) || 0) + 1);
          const price = extractCardPrice(segment);
          if (price != null) prices.push(price);
        }
        out.years_available = [...yearCounts.entries()].map(([year, count]) => ({ year, count }));
        applyPrices(prices);
        return out;
      }
      return out;
    }

    const countMatch =
      text.match(/\bResults:\s*(\d{1,5})\s+Vehicles?\b/i) ||
      text.match(/\b(\d{1,5})\s+vehicles?\s+(?:available|found)\b/i);
    if (countMatch) out.total_count = Number(countMatch[1]) || 0;

    const prices = [];
    for (const m of text.matchAll(/\b(?:Your Price|MSRP|Retail Price|Starting at)\s*\$?\s*([0-9]{1,3}(?:,[0-9]{3})+)/gi)) {
      const n = toNumber(m[1]);
      if (n != null && n >= 1000) prices.push(n);
    }
    applyPrices(prices);

    const yearCounts = new Map();
    const makeModelRe = make && model
      ? new RegExp(`\\b(20\\d{2})\\s+${escapeRegex(make)}\\s+${escapeRegex(model)}\\b`, 'gi')
      : /\b(20\d{2})\s+[A-Z][A-Za-z-]+\s+[A-Z0-9][A-Za-z0-9 -]+/g;
    for (const m of text.matchAll(makeModelRe)) {
      const yr = m[1];
      yearCounts.set(yr, (yearCounts.get(yr) || 0) + 1);
    }
    if (yearCounts.size) {
      out.years_available = [...yearCounts.entries()].map(([year, count]) => ({ year, count }));
    }

    return out;
  } catch (e) {
    out.error = e?.message || String(e);
    out.statusCode = e?.statusCode || e?.response?.statusCode || null;
    return out;
  }
}

// PATCH: model hyphen retry
// If the AI passes a model with no separator between leading letters and the
// first digit (F150, RX350), insert a hyphen and return the normalized form.
// Returns null when no transformation is possible (so callers can skip retry).
// Examples: F150 -> F-150, RX350 -> RX-350, CR-V -> null (already hyphenated),
// Bronco Sport -> null (no digits), RAV4 -> R-AV4 (false positive: retry will
// still 0-out on Marketcheck and we fall through to the original 0-result return).
function maybeHyphenateModel(m) {
  if (!m) return null;
  const m2 = String(m).replace(/^([A-Za-z]+)(\d.*)$/, '$1-$2');
  return m2 !== m ? m2 : null;
}

try {
  const baseUrl = String(supabaseUrl).replace(/\/$/, '');
  const configUrl =
    `${baseUrl}/rest/v1/tenant_inventory_config` +
    `?tenant_id=eq.${encodeURIComponent(String(tenant_id).trim())}` +
    `&select=id,tenant_id,enabled,marketcheck_dealer_id,inventory_url,inventory_url_new,inventory_url_used,inventory_search_url_template,inventory_search_url_template_new,inventory_search_url_template_used,slug_word_separator,platform_id` +
    `&limit=1`;

  const configData = await this.helpers.httpRequest({
    method: 'GET',
    url: configUrl,
    headers: {
      apikey: supabaseKey,
      Authorization: `Bearer ${supabaseKey}`,
      Accept: 'application/json'
    },
    json: true,
    timeout: 8000
  });

  if (!Array.isArray(configData) || configData.length === 0) {
    return JSON.stringify({
      error: 'No tenant_inventory_config row returned from Supabase REST',
      tenant_id_from_lookup_lead: tenant_id,
      supabase_url: supabaseUrl
    });
  }

  row = configData[0];

  if (row.enabled !== true) {
    return JSON.stringify({
      error: 'Config found but not enabled',
      tenant_id_from_lookup_lead: tenant_id,
      row
    });
  }

  if (
    row.marketcheck_dealer_id === null ||
    row.marketcheck_dealer_id === undefined ||
    row.marketcheck_dealer_id === ''
  ) {
    return JSON.stringify({
      error: 'Config found but marketcheck_dealer_id missing',
      tenant_id_from_lookup_lead: tenant_id,
      row
    });
  }

  // PATCH: platform-template resolution + _url placeholders
  // If the tenant points to a shared platform AND has gaps in its own template
  // columns, fetch the platform row and fill the gaps. Tenant columns always
  // win; platform only provides defaults. Soft-fail on any error so existing
  // tenant-only behaviour is preserved.
  if (row.platform_id) {
    try {
      const platformUrl =
        `${baseUrl}/rest/v1/inventory_url_platforms` +
        `?id=eq.${encodeURIComponent(String(row.platform_id).trim())}` +
        `&select=id,url_template_generic,url_template_new,url_template_used,slug_word_separator` +
        `&limit=1`;
      const platformData = await this.helpers.httpRequest({
        method: 'GET',
        url: platformUrl,
        headers: {
          apikey: supabaseKey,
          Authorization: `Bearer ${supabaseKey}`,
          Accept: 'application/json'
        },
        json: true,
        timeout: 8000
      });
      if (Array.isArray(platformData) && platformData.length > 0) {
        const p = platformData[0];
        if (!row.inventory_search_url_template      && p.url_template_generic) row.inventory_search_url_template      = p.url_template_generic;
        if (!row.inventory_search_url_template_new  && p.url_template_new)     row.inventory_search_url_template_new  = p.url_template_new;
        if (!row.inventory_search_url_template_used && p.url_template_used)    row.inventory_search_url_template_used = p.url_template_used;
        // slug_word_separator on the tenant has NOT NULL DEFAULT '-', so it is
        // always populated; we leave it alone (tenant value wins). Platform's
        // value is documentation/onboarding only.
      }
    } catch (e) {
      // Soft-fail: continue with whatever the tenant row already has.
    }
  }

  const baseFilters = { make, model, year, trim, color, maxPrice, maxMiles };

  if (!condition) {
    // No condition specified — fan out to both car_types in parallel.
    const [nRes, uRes] = await Promise.all([
      fetchMarketcheck('new'),
      fetchMarketcheck('used')
    ]);

    if (nRes.error && uRes.error) {
      return JSON.stringify({
        error: 'MarketCheck API error (both calls failed)',
        new_error: nRes.error,
        used_error: uRes.error
      });
    }

    const newSum  = nRes.data ? summarize(nRes.data) : { total_count: 0 };
    const usedSum = uRes.data ? summarize(uRes.data) : { total_count: 0 };

    const newUrl  = buildCustomerInventoryUrl(row, { ...baseFilters, condition: 'new'  }, 'new')
                  || row.inventory_url_new  || row.inventory_url;
    const usedUrl = buildCustomerInventoryUrl(row, { ...baseFilters, condition: 'used' }, 'used')
                  || row.inventory_url_used || row.inventory_url;

    const combinedCount = (newSum.total_count || 0) + (usedSum.total_count || 0);

    if (combinedCount === 0) {
      // PATCH: model hyphen retry
      // Before giving up, try with a hyphenated model (F150 -> F-150, etc.).
      const altModel = maybeHyphenateModel(model);
      if (altModel) {
        const [nRes2, uRes2] = await Promise.all([
          fetchMarketcheck('new',  altModel),
          fetchMarketcheck('used', altModel)
        ]);
        const newSum2  = nRes2.data ? summarize(nRes2.data)  : { total_count: 0 };
        const usedSum2 = uRes2.data ? summarize(uRes2.data) : { total_count: 0 };
        const altCount = (newSum2.total_count || 0) + (usedSum2.total_count || 0);
        if (altCount > 0) {
          const altFilters = { ...baseFilters, model: altModel };
          const newUrlAlt  = buildCustomerInventoryUrl(row, { ...altFilters, condition: 'new'  }, 'new')
                          || row.inventory_url_new  || row.inventory_url;
          const usedUrlAlt = buildCustomerInventoryUrl(row, { ...altFilters, condition: 'used' }, 'used')
                          || row.inventory_url_used || row.inventory_url;
          return JSON.stringify({
            total_count: altCount,
            new:  { ...newSum2,  inventory_url: newUrlAlt  },
            used: { ...usedSum2, inventory_url: usedUrlAlt },
            new_inventory_url:  newUrlAlt,
            used_inventory_url: usedUrlAlt,
            website_url: row.inventory_url,
            model_requested: model,
            model_used: altModel
          });
        }
      }
      const [newWeb, usedWeb] = await Promise.all([
        fetchWebsiteInventorySummary(newUrl, this.helpers, { ...baseFilters, condition: 'new' }),
        fetchWebsiteInventorySummary(usedUrl, this.helpers, { ...baseFilters, condition: 'used' })
      ]);
      const websiteCount = (newWeb.total_count || 0) + (usedWeb.total_count || 0);
      if (websiteCount > 0) {
        return JSON.stringify({
          total_count: websiteCount,
          new:  { ...newWeb,  inventory_url: newUrl  },
          used: { ...usedWeb, inventory_url: usedUrl },
          new_inventory_url:  newUrl,
          used_inventory_url: usedUrl,
          website_url: row.inventory_url,
          source: 'dealer_website_fallback'
        });
      }
      return JSON.stringify({
        total_count: 0,
        new_inventory_url:  newUrl,
        used_inventory_url: usedUrl,
        website_url: row.inventory_url,
        message: "No matching vehicles found in this dealer's inventory."
      });
    }

    return JSON.stringify({
      total_count: combinedCount,
      new:  { ...newSum,  inventory_url: newUrl  },
      used: { ...usedSum, inventory_url: usedUrl },
      new_inventory_url:  newUrl,
      used_inventory_url: usedUrl,
      website_url: row.inventory_url
    });
  }

  // Condition specified — single call, legacy response shape.
  const single = await fetchMarketcheck(condition.toLowerCase());
  if (single.error) {
    return JSON.stringify({
      error: 'MarketCheck API error',
      code: single.error.code,
      message: single.error.message,
      request_url: single.error.request_url
    });
  }

  const sum = summarize(single.data);
  const singleUrl =
    buildCustomerInventoryUrl(row, { ...baseFilters, condition }, single.effCond)
    || row.inventory_url;

  if (sum.total_count === 0) {
    // PATCH: model hyphen retry
    const altModel = maybeHyphenateModel(model);
    if (altModel) {
      const single2 = await fetchMarketcheck(condition.toLowerCase(), altModel);
      if (!single2.error) {
        const sum2 = summarize(single2.data);
        if (sum2.total_count > 0) {
          const altFilters = { ...baseFilters, model: altModel };
          const url2 = buildCustomerInventoryUrl(row, { ...altFilters, condition }, single2.effCond)
                    || row.inventory_url;
          return JSON.stringify({
            ...sum2,
            inventory_url: url2,
            website_url: row.inventory_url,
            model_requested: model,
            model_used: altModel
          });
        }
      }
    }
    const web = await fetchWebsiteInventorySummary(singleUrl, this.helpers, { ...baseFilters, condition });
    if ((web.total_count || 0) > 0) {
      return JSON.stringify({
        ...web,
        inventory_url: singleUrl,
        website_url: row.inventory_url,
        source: 'dealer_website_fallback'
      });
    }
    return JSON.stringify({
      total_count: 0,
      inventory_url: singleUrl,
      website_url: row.inventory_url,
      message: "No matching vehicles found in this dealer's inventory."
    });
  }

  return JSON.stringify({
    ...sum,
    inventory_url: singleUrl,
    website_url: row.inventory_url
  });

} catch (e) {
  return JSON.stringify({
    error: 'Request failed',
    tenant_id_from_lookup_lead: tenant_id,
    supabase_url: supabaseUrl || null,
    config_row: row || null,
    message: e?.message || 'Unknown error',
    statusCode: e?.statusCode || e?.response?.statusCode || null,
    responseBody: e?.response?.body || null
  });
}
