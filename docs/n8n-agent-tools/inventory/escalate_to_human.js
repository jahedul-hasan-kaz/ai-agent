// gate-remediation-protected-tool
// gate-remediation-20260908-v1. Pure capability ownership; no tenant/model policy.
function resolveEngineOwner(classifier = {}, routing = {}, decision = {}) {
  if (classifier.active_appraisal_scheduling === true) return 'scheduling';
  if (classifier.active_buying_request === true) return 'inventory';
  if (classifier.active_finance_question === true) return 'general';
  const specialist = ['scheduling', 'inventory', 'value', 'service_trade_reply'];
  const route = String(routing.route || routing.context_route || '');
  if (specialist.includes(route)) return route;
  if (specialist.includes(String(decision.route || ''))) return decision.route;
  if (classifier.intent === 'scheduling') return 'scheduling';
  if (classifier.intent === 'inventory') return 'inventory';
  if (classifier.intent === 'value_request') return 'value';
  return route || 'general';
}
function isPassiveCallbackContinuation(text = '') {
  return /^(?:thanks[,!. ]*)?(?:i(?: will|'ll) wait for (?:the|your) call|ok(?:ay)?[,!. ]*thanks|thank you)[.!\s]*$/i.test(text);
}
function protectedCloseCategory(classifier = {}, text = '') {
  if (['low_signal','vehicle_unavailable','future_callback','relay_request','missed_callback','hardship_disclosed','credit_denial','deceased','self_harm'].includes(classifier.intent)) return null;
  // A supervisor request is complaint work even if it concerns unwanted messages.
  if (classifier.complaint_signals === true || ['complaint','service_complaint','legal_threat'].includes(classifier.intent)) return null;
  if (classifier.internal_employee === true || classifier.intent === 'internal_employee') return 'internal_employee';
  if (classifier.wrong_owner === true || classifier.intent === 'wrong_owner') return 'wrong_owner';
  // Canonical permission for a requested capability beats a raw repeated-call refusal.
  // This follows the protected identity/safety checks and requires an explicit cleared decline flag.
  if (classifier.explicit_decline_or_deferral === false && (['value_request','inventory','scheduling'].includes(classifier.intent) || classifier.active_buying_request === true || classifier.active_finance_question === true || classifier.active_appraisal_scheduling === true)) return null;
  if (classifier.intent === 'repeat_decline' || /\b(?:already|repeatedly|again|told you)\b/i.test(text) && /\b(?:not interested|no thanks|stop|don'?t want|do not want)\b/i.test(text)) return 'repeat_decline';
  if (classifier.explicit_decline_or_deferral === true || ['not_interested_soft','polite_decline','optin_decline'].includes(classifier.intent)) return 'polite_decline';
  return null;
}
function admitEngineTurn(classifier = {}, routing = {}, decision = {}) {
  const owner = resolveEngineOwner(classifier,routing,decision);
  const protectedIntents = ['complaint','service_complaint','legal_threat','wrong_owner','internal_employee','vehicle_unavailable','vehicle_sold','deceased','hardship_disclosed','credit_denial','self_harm','repeat_decline','not_interested_soft','optin_decline','lease_disclosed','future_callback','relay_request','missed_callback'];
  if (decision.capability === 'compliance' || (decision.reason_codes || []).includes('compliance_preemption')) return {eligible:false,reason:'kernel_compliance_owner'};
  if (protectedIntents.includes(classifier.intent) || classifier.complaint_signals === true || classifier.wrong_owner === true || classifier.internal_employee === true || classifier.deterministic_vehicle_disposal === true || classifier.explicit_decline_or_deferral === true) return {eligible:false,reason:'protected_intent_owner'};
  if (classifier.active_finance_question === true) return {eligible:false,reason:'finance_action_owner'};
  if (['scheduling','inventory','value','service_trade_reply'].includes(owner)) return {eligible:false,reason:'capability_owner_'+owner};
  if (decision.route === 'deterministic' || (decision.tool_commands || []).length) return {eligible:false,reason:'kernel_action_owner'};
  // A general legacy valuation_receipt_unavailable shape is ordinary outreach.
  return {eligible:true,reason:'initial_outreach_general'};
}

const protectedToolCtx=$input.item?.json?.tool_ctx||{};
const protectedToolClassifier=protectedToolCtx.classifier||$input.item?.json?.context_packet?.conversation?.current_inbound?.classifier||(()=>{try{return $('Intent Classifier').first().json||{};}catch(_){return {};}})();
if (isPassiveCallbackContinuation(protectedToolCtx.combined_message||protectedToolCtx.inbound_text||'')) return JSON.stringify({ok:false,error:'passive_callback_continuation_no_new_page',message:'This is only an acknowledgment of a prior callback. Do not page again or claim new delivery.'});
if (protectedCloseCategory(protectedToolClassifier,protectedToolCtx.combined_message||protectedToolCtx.inbound_text||'')) return JSON.stringify({ok:false,error:'protected_close_uses_suppression',message:'Use Suppress_Lead for this protected close. Do not promise a dealer page.'});
// tool_ctx (n8n#20752 fix)
// Escalate_To_Human - Code Tool attached to AI Agent
// Inputs: reason (required), named_person (optional), urgency (normal | high | urgent)
//
// Reads cross-node context from $input.item.json.tool_ctx (populated by the
// `Tool Context` Code node upstream of the AI Agent). Avoids `$('OtherNode')`
// IPC reads inside this tool, which can hang the runner per n8n#20752.

const _ctx = ($input.item && $input.item.json && $input.item.json.tool_ctx) || {};

const args = (typeof query === 'object' && query !== null) ? query : {};
const fallback = ($input.item && $input.item.json) ? $input.item.json : {};
const reason = String(args.reason || fallback.reason || args.query || fallback.query || '').trim();
const namedPersonRaw = args.named_person ?? args.namedPerson ?? fallback.named_person ?? fallback.namedPerson ?? null;
const namedPerson = namedPersonRaw ? String(namedPersonRaw).trim() : null;

// named-person-shape-v1 (2026-08-28, row ff17f9c9): named_person must be a real
// name or a dealership role before it is written, categorised, or echoed back.
// Strips non-letters, iteratively drops leading prepositions/articles/possessives,
// caps at 3 tokens; accepts pure dealership-role values; rejects any value
// carrying a stopword (prepositions, relatives, AI/bot words, mid-sentence
// fragments); requires at least one name-shaped token. Rejection returns ''.
function sanitizeNamedPerson(raw) {
  const ROLE_TOKENS = new Set(['manager', 'managers', 'sales', 'service', 'finance', 'parts', 'team', 'supervisor', 'owner', 'boss', 'gm']);
  const STOP_TOKENS = new Set(['to', 'in', 'at', 'for', 'with', 'about', 'from', 'of', 'on', 'the', 'a', 'an', 'my', 'our', 'your', 'every', 'chat', 'check', 'call', 'text', 'back', 'now', 'wife', 'husband', 'partner', 'spouse', 'family', 'friend', 'person', 'people', 'human', 'ai', 'bot', 'robot', 'someone', 'somebody', 'anyone', 'anybody', 'everyone', 'everybody', 'nobody', 'them', 'they', 'you', 'me', 'him', 'her', 'us', 'morning', 'afternoon', 'evening', 'tonight', 'today', 'tomorrow', 'time', 'number', 'auto', 'generated', 'tried', 'would', 'freed', 'up', 'this', 'that', 'it', 'one']);
  let s = String(raw || '').replace(/[^A-Za-z' -]/g, ' ').replace(/\s+/g, ' ').trim();
  if (!s) return '';
  let prevS = null;
  while (s && s !== prevS) {
    prevS = s;
    s = s.replace(/^(?:to|in|at|for|with|about|from|of|on|the|a|an|my|our|your)\s+/i, '');
  }
  const tokens = s.split(/\s+/).filter(Boolean).slice(0, 3);
  if (!tokens.length) return '';
  if (tokens.every(t => ROLE_TOKENS.has(t.toLowerCase()))) return tokens.join(' ');
  if (tokens.some(t => STOP_TOKENS.has(t.toLowerCase()))) return '';
  if (!tokens.some(t => /^[a-z][a-z'-]{1,29}$/i.test(t))) return '';
  return tokens.join(' ');
}
const namedPersonValid = sanitizeNamedPerson(namedPerson);
const rawUrgency = String(args.urgency || fallback.urgency || 'normal').trim().toLowerCase();
// permanent-dnc-halt-hold-v1 (2026-09-06, adversarial review D2-V1 / D2b-1; Nick decision 2, option 2 build):
// `permanent_dnc` used to be a MODEL ARGUMENT that wrote a dnc row, archived + blocked the lead and set
// lead_cadence 'dnc' with no authorization at all (31 of 34 free-text-reason DNCs in 60d had no STOP-class
// inbound). Model/tool arguments are requests, not authorization (suppression-mutation-authorization-v2 in
// Suppress_Lead). This tool no longer writes dnc / leads / lead_cadence. Deceased-owner and ownership-
// disputed DETECTIONS (deterministic classifier evidence, never the argument) route through
// public.apply_lead_suppression with p_permanent_dnc=false: halt + hold is the ceiling. The legacy argument
// is read only so the receipt can say it was ignored; the inputSchema no longer offers it.
const legacyPermanentDncRequested = !!(args.permanent_dnc ?? args.permanentDnc ?? fallback.permanent_dnc ?? fallback.permanentDnc);

const tenantId = _ctx.tenant_id;
const leadId = _ctx.lead_id;
const leadPhone = _ctx.lead_phone;
const inbound = _ctx.inbound_text;
const msgs = _ctx.recent_messages || [];
const tenant = _ctx.tenant_emails_phone || {};

const urgency = ['normal', 'high', 'urgent'].includes(rawUrgency) ? rawUrgency : 'normal';

if (!reason) {
  return JSON.stringify({ ok: false, error: 'reason is required' });
}

// deferral-gate-v1 (2026-08-28, row ff17f9c9): a soft deferral ("I will have to
// get back to you", "let me talk to my wife") with no valid named person is not
// an escalation trigger. Consume known deferral/closing chunks from the front
// until fixpoint; the gate fires only when the whole turn is consumed and at
// least one DEFERRAL chunk was seen (same sawSignal discipline as the live
// pivotClosingByShape consumer in Empty Reply Detector).
// The CLOSING chunk bodies below are verbatim copies of the alternation bodies
// of PIVOT_CLOSING_SIGNAL_RE / PIVOT_CLOSING_FILLER_RE in the live Empty Reply
// Detector node. A toolCode cannot import from another node, so the offline
// suite pins both copies against that node's source to catch drift.
const DEFERRAL_CHUNK_RE = new RegExp('^(?:' + [
  "(?:i|we)(?:'| )?(?:ll|will|would|d|m)?",
  'let me',
  'have to|need to|got to|gotta|going to|gonna|probably|maybe|just|might|may',
  'get back (?:to|with) (?:you|ya)(?: on (?:that|this)(?: one)?)?',
  'let you know',
  'think (?:about|on|it over)(?: it| this| that)?',
  'sleep on it',
  '(?:talk|speak|chat|check|discuss) (?:it over |this |that )?(?:to|with) (?:my|our|the) (?:wife|husband|partner|spouse|family|kids?|son|daughter|dad|mom|father|mother|brother|sister)',
  'run it by (?:my|our) \\w+',
  'after (?:speaking|talking|checking) (?:to|with) (?:my|our) \\w+',
  'on (?:that|this)(?: one)?',
  'for now|right now|at the moment|at this time|later|soon|eventually|at some point|down the road',
  'next (?:week|month|year|time)',
  'when (?:i|we) (?:bring|take|get) (?:my|the) \\w+ in',
  'schedule something|set something up|wait',
  'to|and|but|so|then',
].join('|') + ')\\b\\s*');
const CLOSING_SIGNAL_CHUNK_RE = new RegExp('^(?:' + [
  'thanks|thank you|thank u|thank ya|thankyou|thanx|thx|tks|txs|thks|thnx|ty'
    + '|much appreciated|many thanks',
  '(?:i |we )?(?:really |truly |much )?appreciate'
    + '(?: (?:it|that|you|the help|your help|the info(?:rmation)?|you all))?',
  '(?:you|u) (?:too|two|to|as well)|same to you|(?:and )?you (?:do )?the same',
  'you do the same|(?:you|u|your|youre|you re|ur) (?:are )?welcome',
  'bye|goodbye|later|talk soon|talk to you (?:soon|later)|ttyl|cheers',
  '(?:you )?(?:take care|be safe|stay safe|god bless|bless you)(?: (?:too|as well|also))?',
  '(?:have|enjoy) (?:a|an|the|your) (?:\\w+ ){0,3}(?:rest of (?:your|the) )?(?:\\w+ ){0,2}'
    + '(?:day|night|one|evening|morning|afternoon|weekend|week)',
].join('|') + ')\\b\\s*');
const CLOSING_FILLER_CHUNK_RE = new RegExp('^(?:' + [
  'ok(?:ay)?|okey|kk|alright(?:y)?|yes|yep|yup|ya|yeah|sure(?: thing)?|no problem|np',
  'will do|wilco|can do',
  'sounds? (?:good|great)|got it|good|great|perfect|awesome|cool|excellent|lol|haha',
  'copy(?: that)?|roger(?: that)?|10 4|gotcha|understood|noted|bet|word',
  'though|anyway|anyhow|again|so (?:much|very much)|very much|a lot|a bunch|kindly|much',
  'for (?:the |your |all |all the )?(?:\\w+ ){0,2}'
    + '(?:reply|response|help|info(?:rmation)?|update|time|call|text|message|asking'
    + '|reaching out|checking in|letting me know|everything)',
  'and|also|too|as well|mr|mrs|ms',
  '(?:my )?(?:friend|man|dude|bro(?:ther)?|buddy|pal|boss|chief|fam|sir|maam|ma am)',
].join('|') + ')\\b\\s*');
function isDeferralOnly(text) {
  let rest = String(text || '').toLowerCase().replace(/[^a-z0-9\s]/g, ' ').replace(/\s+/g, ' ').trim();
  if (!rest) return false;
  let sawDeferral = false;
  let prevRest = null;
  while (rest && rest !== prevRest) {
    prevRest = rest;
    if (DEFERRAL_CHUNK_RE.test(rest)) {
      sawDeferral = true;
      rest = rest.replace(DEFERRAL_CHUNK_RE, '');
      continue;
    }
    rest = rest.replace(CLOSING_SIGNAL_CHUNK_RE, '');
    rest = rest.replace(CLOSING_FILLER_CHUNK_RE, '');
  }
  return sawDeferral && rest === '';
}
if (!namedPersonValid && isDeferralOnly(inbound)) {
  return JSON.stringify({ ok: false, blocked: 'deferral_no_escalation', message: 'The customer is deferring, not asking to reach a person. Do not escalate and do not claim the team was notified. Reply with one brief, pressure-free acknowledgment that they can reach out anytime.' });
}

function escalationTimeZone() {
  const timezone = (tenant.hours_of_operation && tenant.hours_of_operation.timezone) || tenant.timezone || tenant.time_zone || _ctx.timezone || 'America/New_York';
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: timezone }).format(new Date());
    return timezone;
  } catch {
    return 'America/New_York';
  }
}

function formatEscalationSummaryLine(message) {
  const raw = message.created_at || message.createdAt || message.timestamp || message.sent_at || message.updated_at || '';
  const date = new Date(raw);
  const stamp = raw && !Number.isNaN(date.getTime())
    ? new Intl.DateTimeFormat('en-US', {
        timeZone: escalationTimeZone(),
        month: '2-digit',
        day: '2-digit',
        year: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
        hour12: true,
      }).format(date)
    : 'date/time unavailable';
  const speaker = message.direction === 'inbound' ? 'Customer' : 'AI';
  return `[${stamp}] ${speaker}: ${message.message || ''}`;
}

const summary = msgs.slice(-5)
  .map(formatEscalationSummaryLine)
  .join('\n');

const supabaseUrl = String($vars.SUPABASE_URL || '').replace(/\/$/, '');
const supabaseKey = $vars.SUPABASE_SERVICE_ROLE_KEY;

if (!supabaseUrl || !supabaseKey) {
  return JSON.stringify({
    ok: false,
    error: 'Missing Supabase URL or service role key in n8n vars',
    hasSupabaseUrl: !!supabaseUrl,
    hasSupabaseKey: !!supabaseKey
  });
}

// Fix 3 (escalation-category-semantic-2026-07-05): category is SEMANTIC (what the issue is),
// independent of urgency (severity). Values must stay within the
// escalations_category_check whitelist. Fallback: a named_person implies
// a relay ask; otherwise 'other' (never default to relay_request).
// escalate-tool-schema-service-complaint-v1 (2026-09-04, row cf53d0a2; fallback recurrences d8c64ac6/41b22b80): inputSchema enum now matches this list.
const ALLOWED_CATEGORIES = ['relay_request', 'missed_callback', 'complaint', 'refund', 'value_request', 'vehicle_unavailable', 'service_complaint', 'other'];
const rawCategory = String(args.category || fallback.category || '').trim().toLowerCase();
const category = ALLOWED_CATEGORIES.includes(rawCategory)
  ? rawCategory
  : (namedPersonValid ? 'relay_request' : 'other');

// low-signal-tool-gate-v1 (2026-08-31, escalation-narrowing build): a bare
// affirmative / courtesy-only turn ("yes", "sure", "ok sounds good",
// "thanks!") consumes entirely via the CLOSING chunk regexes with no deferral
// seen, so deferral-gate-v1 deliberately does not fire on it - and nothing
// else here blocked it (14d: 29 relay + 23 value + 18 'other' tool rows fired
// on bare affirmatives). Nick's rule: a bare affirmative is NEVER by itself
// grounds to page a dealer. The one legitimate shape is accepting a contact
// offer we actually made, honored via tool_ctx.last_outbound_message
// (toolctx-last-outbound-v1). Complaint classes, urgent calls and valid named
// persons are never blocked. The extra chunk regex below is additive and
// local to this gate; the pinned CLOSING copies stay byte-identical to the
// Empty Reply Detector originals.
const LOW_SIGNAL_EXTRA_CHUNK_RE = /^(?:please|pls|plz|absolutely|definitely|of course)\b\s*/;
function isClosingFillerOnly(text) {
  const rawTurn = String(text || '').trim();
  if (!rawTurn) return false;
  let rest = rawTurn.toLowerCase().replace(/[^a-z0-9\s]/g, ' ').replace(/\s+/g, ' ').trim();
  // A non-empty turn with no letters or digits at all (a lone emoji, "!!") is
  // a reaction, not a request to reach a person.
  if (!rest) return true;
  let prevRest = null;
  while (rest && rest !== prevRest) {
    prevRest = rest;
    rest = rest.replace(CLOSING_SIGNAL_CHUNK_RE, '');
    rest = rest.replace(CLOSING_FILLER_CHUNK_RE, '');
    rest = rest.replace(LOW_SIGNAL_EXTRA_CHUNK_RE, '');
  }
  return rest === '';
}
const _lastOutboundMessage = String(_ctx.last_outbound_message || '');
// accepted-offer-cut-v1 (2026-08-31, build 2): content offers (send options /
// numbers / pricing, have-the-team-flag-one, want-me-to-set-that-up) no longer
// unblock a bare affirmative - Nick cut the accepted-options-offer page. Only
// CONTACT offers (a call, or having someone reach out) remain a legitimate
// acceptance. Scheduling acceptances book; content acceptances get answered
// by the agent itself. Side effect, intended: "I'll have the team follow up"
// acks are not offers, so a trailing thanks can never re-page.
const _contactOfferRe = /\b(?:have\s+(?:someone|somebody|the\s+team|a\s+(?:manager|buyer|specialist)|our\s+(?:team|managers?|buyers?))\b[^.?!\n]{0,40}\b(?:call|phone|reach\s+out)\b|when\s+would\s+be\s+a\s+(?:convenient|good|best)\s+time\s+for\s+a\s+call|i\s+can\s+have\s+someone\s+(?:call|reach\s+out)|give\s+you\s+a\s+call|quick\s+call\s+with\s+one\s+of\s+our\s+buyers|(?:good|best|convenient)\s+time\s+(?:for\s+(?:a|the)\s+call|to\s+(?:call|reach)\s+you))\b/i;
if (!namedPersonValid
    && urgency !== 'urgent'
    && !['complaint', 'service_complaint', 'refund'].includes(category)
    && isClosingFillerOnly(inbound)
    && !_contactOfferRe.test(_lastOutboundMessage)) {
  return JSON.stringify({ ok: false, blocked: 'low_signal_no_escalation', message: 'The customer sent a brief acknowledgment, not a request to reach a person. Do not escalate and do not claim the team was notified. Answer the thread or close warmly.' });
}

// ta-active-intent-20260908-v1: shared sales-handoff PK with final receipt guard.
const activeSaved = _ctx.saved_inbound || {};
const activeSales = ['other','relay_request','value_request'].includes(category);
let activeId = null;
if (activeSales) {
  const rawId = String(activeSaved.id || '');
  if (String(activeSaved.tenant_id || '') !== String(tenantId || '') || String(activeSaved.lead_id || '') !== String(leadId || '') || activeSaved.direction !== 'inbound' || !/^[1-9]\d*$/.test(rawId) || (typeof activeSaved.id === 'number' && !Number.isSafeInteger(activeSaved.id)) || BigInt(rawId) > 9223372036854775807n) return JSON.stringify({ok:false,error:'missing_current_saved_inbound',message:'No follow-up was verified. Ask the customer to contact the dealership directly.'});
  const h = BigInt(rawId).toString(16).padStart(16,'0');
  activeId = 'a1080908-' + h.slice(0,4) + '-4' + h.slice(4,7) + '-8' + h.slice(7,10) + '-000000' + h.slice(10);
}
const activeExact = rows => Array.isArray(rows) && rows.length === 1 && rows[0].id === activeId && rows[0].tenant_id === tenantId && rows[0].lead_id === leadId && rows[0].source === 'sms' && ['other','relay_request','value_request'].includes(rows[0].category) && rows[0].lane === 'sales' && rows[0].origin === 'customer';
const activeRead = () => this.helpers.httpRequest({method:'GET',url:`${supabaseUrl}/rest/v1/escalations?id=eq.${encodeURIComponent(activeId)}&select=id,tenant_id,lead_id,source,category,lane,origin,status&limit=2`,headers:{apikey:supabaseKey,Authorization:`Bearer ${supabaseKey}`},json:true,timeout:10000});

const body = {
  ...(activeId ? {id:activeId} : {}),
  tenant_id: tenantId,
  lead_id: leadId,
  source: 'sms',
  // escalation-lane-routing-v1: agent-chosen escalation, so always customer-origin.
  lane: category === 'service_complaint' ? 'service' : 'sales',
  origin: 'customer',
  category: category,
  inbound_message: inbound,
  conversation_summary:
    `[AI escalation] reason=${reason}; category=${category}; named_person=${namedPersonValid || 'n/a'}; ` +
    `urgency=${urgency}\n\nLast 5 messages:\n${summary}`
};

try {
  let activeExisting = activeId ? await activeRead() : [];
  if (activeId && activeExisting.length && !activeExact(activeExisting)) throw new Error('persisted_receipt_binding_mismatch');
  const resp = activeId && activeExact(activeExisting) ? activeExisting : await this.helpers.httpRequest({
    method: 'POST',
    url: `${supabaseUrl}/rest/v1/escalations${activeId ? '?on_conflict=id' : ''}`,
    headers: {
      apikey: supabaseKey,
      Authorization: `Bearer ${supabaseKey}`,
      'Content-Type': 'application/json',
      Prefer: activeId ? 'resolution=ignore-duplicates,return=representation' : 'return=representation'
    },
    body,
    json: true,
    timeout: 10000
  });

  const activeVerified = activeId ? await activeRead() : null;
  if (activeId && !activeExact(activeVerified)) throw new Error('persisted_receipt_not_verified');
  const row = activeId ? activeVerified[0] : (Array.isArray(resp) ? resp[0] : resp);

  // permanent-dnc-halt-hold-v1: authorization contract shared with Suppress_Lead. Evidence comes from the
  // deterministic classifier (context packet first, static node read as the fallback), never from the model.
  // Only two detections suppress here, and only as halt + hold (p_permanent_dnc=false): deceased owner and
  // ownership disputed. Anything else, including a legacy permanent_dnc=true argument, writes nothing.
  const classifier = (() => {
    const packetClassifier = fallback.context_packet?.conversation?.current_inbound?.classifier;
    if (packetClassifier && typeof packetClassifier === 'object') return packetClassifier;
    try {
      const nodeClassifier = $('Intent Classifier').first().json;
      return nodeClassifier && typeof nodeClassifier === 'object' ? nodeClassifier : {};
    } catch (_) {
      return {};
    }
  })();
  const deceasedDetected = classifier.deceased === true;
  const wrongOwnerDetected = classifier.wrong_owner === true || classifier.intent === 'wrong_owner';
  const suppressionCategory = deceasedDetected ? 'deceased' : (wrongOwnerDetected ? 'wrong_owner' : null);
  const suppression = {
    schema_version: 'escalate_to_human_suppression.v1',
    legacy_permanent_dnc_requested: legacyPermanentDncRequested,
    legacy_permanent_dnc_honored: false,
    category: suppressionCategory,
    attempted: false,
    ok: false,
    permanent_dnc: false,
    dnc_applied: false,
    cadences_halted: 0,
    hold_applied: false,
    suppression_effective: false,
    nothing_written: true,
    error: null,
    result: null
  };
  if (suppressionCategory && leadId && tenantId) {
    suppression.attempted = true;
    try {
      const supResp = await this.helpers.httpRequest({
        method: 'POST',
        url: `${supabaseUrl}/rest/v1/rpc/apply_lead_suppression`,
        headers: {
          apikey: supabaseKey,
          Authorization: `Bearer ${supabaseKey}`,
          'Content-Type': 'application/json'
        },
        body: {
          p_lead_id: leadId,
          p_tenant_id: tenantId,
          p_reason: ('escalate_to_human:' + suppressionCategory + ': ' + reason).slice(0, 500),
          p_permanent_dnc: false,
          p_vehicle_disposed: false,
          p_vehicle_id: null,
          p_suppression_category: suppressionCategory
        },
        json: true,
        timeout: 10000
      });
      const r = (supResp && typeof supResp === 'object' && !Array.isArray(supResp)) ? supResp : {};
      suppression.result = r;
      suppression.ok = r.success === true;
      // dnc_applied / permanent_dnc mirror the RPC: with p_permanent_dnc=false they stay false.
      suppression.dnc_applied = r.dnc_applied === true;
      suppression.permanent_dnc = r.dnc_applied === true;
      suppression.cadences_halted = Number.isInteger(r.cadences_halted) ? r.cadences_halted : 0;
      suppression.hold_applied = r.hold_applied === true;
      suppression.suppression_effective = suppression.ok
        && (r.suppression_effective === true || suppression.dnc_applied || suppression.cadences_halted > 0
          || suppression.hold_applied);
      suppression.nothing_written = !suppression.suppression_effective;
    } catch (supErr) {
      suppression.error = supErr?.message || 'apply_lead_suppression failed';
    }
  }
  // The note is scoped to THIS tool's result: Suppress_Lead may have written the suppression on the same turn
  // (deceased is a Suppress_Lead category and an Escalate_To_Human compliance case), so a no-op here must never
  // read as a global 'nothing was suppressed' or contradict a Suppress_Lead receipt.
  const suppressionNote = !suppression.attempted
    ? (legacyPermanentDncRequested
        ? ' permanent_dnc was IGNORED: this tool never DNCs a contact and wrote no suppression. Do NOT claim the customer is off the list or that messages will stop on the strength of THIS result; if suppression is warranted, call Suppress_Lead with its category and follow its receipt.'
        : '')
    : suppression.suppression_effective
      ? ` Active cadences were halted and a send hold is in place for this contact (${suppression.category}; no DNC row). Do NOT claim the customer was unsubscribed, removed or DNC'd.`
      : ` This tool wrote no suppression for this contact (${suppression.category}${suppression.error ? ': ' + suppression.error : ''}). Do NOT claim the customer is off the list or that messages will stop on the strength of THIS result; only a Suppress_Lead receipt with suppression_effective=true licenses that.`
        + (suppression.category === 'deceased'
          ? ' Suppress_Lead(suppression_category=deceased) owns the deceased suppression.' : '');

  return JSON.stringify({
    ok: true,
    permanent_dnc: suppression.permanent_dnc,
    dnc_applied: suppression.dnc_applied,
    suppression,
    escalation_id: row?.id || null,
    ...(activeId ? {tenant_id:tenantId,lead_id:leadId,inbound_log_id:String(activeSaved.id),action:'sales_handoff',dealer_delivery_verified:false} : {}),
    category: row?.category || body.category,
    urgency,
    named_person: namedPersonValid || null,
    message: 'The request is saved for dealership review. Do not claim notification delivery or promise a call time.' + suppressionNote
  });
} catch (e) {
  return JSON.stringify({
    ok: false,
    error: e?.message || 'Escalation insert failed',
    statusCode: e?.statusCode || e?.response?.statusCode || null,
    responseBody: e?.response?.body || null
  });
}
