import type { DashboardPayload, MarketIntelligenceOverview } from '../types/certainty';

export type GeminiRoleId =
  | 'CRYPTO_MACRO'
  | 'EQUITIES_CROSS_ASSET'
  | 'RISK_CALIBRATION'
  | 'FAST_DESK';

export type GeminiTaskMode =
  | 'DEEP_REASONING'
  | 'COMPLEX_INTERACTIVE'
  | 'GENERAL_SEARCH'
  | 'FAST_LITE';

export type GeminiThinkingTier = 'XHIGH' | 'HIGH' | 'MEDIUM' | 'LOW' | 'MINIMAL';

export interface GeminiRoleDefinition {
  id: GeminiRoleId;
  label: string;
  subtitle: string;
  defaultTaskMode: GeminiTaskMode;
  defaultSearchEnabled: boolean;
  systemInstruction: string;
  starterPrompts: string[];
}

export interface GeminiTaskRouteConfig {
  mode: GeminiTaskMode;
  label: string;
  model: 'gemini-4-hydrogen' | 'gemini-3.1-flash-lite';
  thinkingLevel?: GeminiThinkingTier;
  supportsSearchGrounding: boolean;
  description: string;
}

export interface GroundingSource {
  title: string;
  uri: string;
  domain: string;
}

export interface GeminiChatTurn {
  role: 'user' | 'model';
  text: string;
}

const ADVISORY_SAFETY_GUARDRAIL = [
  'OPERATIONAL BOUNDARIES:',
  '- You are an advisory quantitative research analyst inside The Certainty Stack.',
  '- Paper trading and research only: live trading is permanently disabled (live_trading_enabled = false).',
  '- Never advise overriding or bypassing the DeterministicRiskGate or the Kill Switch.',
  '- Never treat simulated, test, or unverified assumptions as verified live evidence.',
  '- Keep analysis concise, structured, quantitative, and explicit about uncertainty and provenance.',
].join('\n');

export const GEMINI_ROLE_DEFINITIONS: Record<GeminiRoleId, GeminiRoleDefinition> = {
  CRYPTO_MACRO: {
    id: 'CRYPTO_MACRO',
    label: 'Crypto & Macro Strategist',
    subtitle: 'BTC/ETH catalysts, ETF flows, OKX funding, Fed/macro drivers',
    defaultTaskMode: 'GENERAL_SEARCH',
    defaultSearchEnabled: true,
    systemInstruction: [
      'Role: Crypto & Macro Strategist for The Certainty Stack.',
      'Focus: Explain WHY BTC and ETH prices fluctuate (ETF inflows/outflows like IBIT/FBTC, OKX funding/long-short crowding, liquidations, spot volume surges, Federal Reserve announcements, geopolitical risk) and WHEN volatility compression or catalysts imply elevated movement probability.',
      'Compare Crypto AI Council verdicts with live web search catalysts when Search Grounding is active.',
      ADVISORY_SAFETY_GUARDRAIL,
    ].join('\n\n'),
    starterPrompts: [
      'What macro, ETF flow, or Fed catalysts are driving Bitcoin and Ethereum volatility today?',
      'Analyze whether current OKX funding crowding and Fear & Greed point toward a volatility expansion.',
      'Compare BTC correlation against QQQ/SPY and explain what breaks the decoupling.',
    ],
  },
  EQUITIES_CROSS_ASSET: {
    id: 'EQUITIES_CROSS_ASSET',
    label: 'Equities & Cross-Asset Analyst',
    subtitle: 'SPY/QQQ/IBIT correlations, Treasury/macro drivers & Stocks Council',
    defaultTaskMode: 'COMPLEX_INTERACTIVE',
    defaultSearchEnabled: true,
    systemInstruction: [
      'Role: Equities & Cross-Asset Analyst for The Certainty Stack.',
      'Focus: Evaluate SPY, QQQ, IBIT, macro economic releases (CPI, PPI, payrolls, FOMC), earnings/rate sensitivity, and Pearson correlations with digital assets.',
      'Synthesize how Stocks AI Council positions align or diverge from current market news.',
      ADVISORY_SAFETY_GUARDRAIL,
    ].join('\n\n'),
    starterPrompts: [
      'Summarize the latest Fed and US macro drivers affecting SPY, QQQ, and IBIT today.',
      'How do Treasury yields and tech equity breadth influence crypto risk-on regimes right now?',
      'Review the Stocks AI Council stance alongside current equity market headlines.',
    ],
  },
  RISK_CALIBRATION: {
    id: 'RISK_CALIBRATION',
    label: 'Risk & Calibration Auditor',
    subtitle: 'Polymarket/Kalshi arbitrage math, Brier scores, Risk Gate audits',
    defaultTaskMode: 'DEEP_REASONING',
    defaultSearchEnabled: false,
    systemInstruction: [
      'Role: Risk & Calibration Auditor for The Certainty Stack.',
      'Focus: Inspect intra-venue Polymarket CLOB V2 spreads, Kalshi cross-venue settlement equivalence rules, taker fees/slippage math, DeterministicRiskGate denials, Brier score / ECE calibration, and contamination quarantine of TEST/SIMULATED predictions.',
      'Apply strict statistical skepticism and point out sample-size insufficiency before claiming model skill.',
      ADVISORY_SAFETY_GUARDRAIL,
    ].join('\n\n'),
    starterPrompts: [
      'Audit our current live calibration Brier score and explain how sample size affects confidence.',
      'Explain why the Risk Gate blocks cross-venue pairs without verified settlement equivalence.',
      'Walk through the net edge formula after Polymarket taker fees and slippage buffers.',
    ],
  },
  FAST_DESK: {
    id: 'FAST_DESK',
    label: 'Fast Market Desk',
    subtitle: 'Rapid Q&A, quick definitions & concise data checks',
    defaultTaskMode: 'FAST_LITE',
    defaultSearchEnabled: false,
    systemInstruction: [
      'Role: Fast Market Desk Assistant for The Certainty Stack.',
      'Focus: Provide concise, crisp, direct answers on market terminology, prediction market mechanics, order book metrics, and platform workflow without unnecessary prose.',
      ADVISORY_SAFETY_GUARDRAIL,
    ].join('\n\n'),
    starterPrompts: [
      'Summarize our current workspace status and Risk Gate posture in 4 bullet points.',
      'What is the difference between Brier score and Expected Calibration Error (ECE)?',
      'What does leg imbalance ratio measure in a binary YES+NO order book fill?',
    ],
  },
};

export const GEMINI_TASK_ROUTES: Record<GeminiTaskMode, GeminiTaskRouteConfig> = {
  DEEP_REASONING: {
    mode: 'DEEP_REASONING',
    label: 'Deep Reasoning (Hydrogen XHIGH)',
    model: 'gemini-4-hydrogen',
    thinkingLevel: 'XHIGH',
    supportsSearchGrounding: true,
    description: 'Maximum reasoning depth for multi-factor root-cause and calibration audits.',
  },
  COMPLEX_INTERACTIVE: {
    mode: 'COMPLEX_INTERACTIVE',
    label: 'Complex Analysis (Hydrogen HIGH)',
    model: 'gemini-4-hydrogen',
    thinkingLevel: 'HIGH',
    supportsSearchGrounding: true,
    description: 'Balanced high-depth reasoning for multi-turn council and cross-asset analysis.',
  },
  GENERAL_SEARCH: {
    mode: 'GENERAL_SEARCH',
    label: 'General + Search (Hydrogen)',
    model: 'gemini-4-hydrogen',
    thinkingLevel: 'MEDIUM',
    supportsSearchGrounding: true,
    description: 'Standard synthesis paired with Google Search grounding for live catalysts.',
  },
  FAST_LITE: {
    mode: 'FAST_LITE',
    label: 'Fast Response (Flash Lite)',
    model: 'gemini-3.1-flash-lite',
    supportsSearchGrounding: false,
    description: 'Lowest-latency tier for quick lookups, definitions, and brief desk checks.',
  },
};

export function resolveGeminiTaskRoute(mode: string | undefined): GeminiTaskRouteConfig {
  if (mode && mode in GEMINI_TASK_ROUTES) {
    return GEMINI_TASK_ROUTES[mode as GeminiTaskMode];
  }
  return GEMINI_TASK_ROUTES.GENERAL_SEARCH;
}

export function resolveGeminiRole(roleId: string | undefined): GeminiRoleDefinition {
  if (roleId && roleId in GEMINI_ROLE_DEFINITIONS) {
    return GEMINI_ROLE_DEFINITIONS[roleId as GeminiRoleId];
  }
  return GEMINI_ROLE_DEFINITIONS.CRYPTO_MACRO;
}

export function sanitizeChatHistory(
  rawHistory: unknown,
  maxTurns = 24,
  maxCharsPerTurn = 4000
): GeminiChatTurn[] {
  if (!Array.isArray(rawHistory)) return [];
  const cleaned: GeminiChatTurn[] = [];
  for (const item of rawHistory) {
    if (!item || typeof item !== 'object') continue;
    const record = item as Record<string, unknown>;
    const role = record.role === 'model' ? 'model' : record.role === 'user' ? 'user' : null;
    const rawText = typeof record.text === 'string' ? record.text.trim() : '';
    if (!role || !rawText) continue;
    cleaned.push({
      role,
      text: rawText.slice(0, maxCharsPerTurn),
    });
  }
  return cleaned.slice(-Math.max(1, maxTurns));
}

export function extractGroundingSources(groundingMetadata: unknown): {
  sources: GroundingSource[];
  searchQueries: string[];
} {
  const sources: GroundingSource[] = [];
  const seenUris = new Set<string>();
  const searchQueries: string[] = [];

  if (!groundingMetadata || typeof groundingMetadata !== 'object') {
    return { sources, searchQueries };
  }

  const meta = groundingMetadata as {
    groundingChunks?: Array<{ web?: { uri?: string; title?: string } }>;
    webSearchQueries?: unknown[];
  };

  if (Array.isArray(meta.webSearchQueries)) {
    for (const q of meta.webSearchQueries) {
      if (typeof q === 'string' && q.trim()) {
        searchQueries.push(q.trim());
      }
    }
  }

  if (Array.isArray(meta.groundingChunks)) {
    for (const chunk of meta.groundingChunks) {
      const uri = chunk?.web?.uri?.trim();
      if (!uri) continue;
      let parsed: URL;
      try {
        parsed = new URL(uri);
      } catch {
        continue;
      }
      if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
        continue;
      }
      if (seenUris.has(parsed.href)) continue;
      seenUris.add(parsed.href);
      const title = (chunk?.web?.title ?? '').trim() || parsed.hostname;
      sources.push({
        title,
        uri: parsed.href,
        domain: parsed.hostname.replace(/^www\./, ''),
      });
    }
  }

  return { sources, searchQueries };
}

export function buildWorkspaceContextSummary(params: {
  marketIntel?: MarketIntelligenceOverview | null;
  dashboard?: DashboardPayload | null;
}): string {
  const lines: string[] = [];
  const snap = params.marketIntel?.latest_snapshot;

  if (snap) {
    const btc = snap.assets?.BTC;
    const eth = snap.assets?.ETH;
    const spy = snap.assets?.SPY;
    const qqq = snap.assets?.QQQ;
    const ibit = snap.assets?.IBIT;
    const okxBtc = snap.derivatives?.BTC;
    lines.push(
      `Market Snapshot ${snap.snapshot_id} (${snap.provenance}, ${snap.feed_health.live_count}/${snap.feed_health.total_count} live feeds, ${snap.created_at}):`
    );
    if (btc?.price_usd != null) {
      lines.push(`- BTC: $${btc.price_usd.toFixed(0)} (24h ${btc.change_24h_pct?.toFixed(2) ?? 'n/a'}%)`);
    }
    if (eth?.price_usd != null) {
      lines.push(`- ETH: $${eth.price_usd.toFixed(0)} (24h ${eth.change_24h_pct?.toFixed(2) ?? 'n/a'}%)`);
    }
    if (spy?.price_usd != null || qqq?.price_usd != null || ibit?.price_usd != null) {
      lines.push(
        `- Equities/ETF: SPY $${spy?.price_usd?.toFixed(2) ?? 'n/a'}, QQQ $${qqq?.price_usd?.toFixed(2) ?? 'n/a'}, IBIT $${ibit?.price_usd?.toFixed(2) ?? 'n/a'}`
      );
    }
    if (snap.correlations?.BTC_QQQ?.correlation != null) {
      lines.push(`- BTC<->QQQ Pearson r=${snap.correlations.BTC_QQQ.correlation.toFixed(2)} (${snap.correlations.BTC_QQQ.regime})`);
    }
    if (snap.fear_greed_value != null) {
      lines.push(`- Fear & Greed: ${snap.fear_greed_value}/100 (${snap.sentiment?.classification ?? 'n/a'})`);
    }
    if (okxBtc?.funding_annualized_pct != null) {
      lines.push(
        `- BTC Perps (OKX): annualized funding ${okxBtc.funding_annualized_pct}%, L/S ratio ${okxBtc.long_short_account_ratio ?? 'n/a'}, regime ${okxBtc.crowding_regime ?? 'n/a'}`
      );
    }
    lines.push(
      `- Movement Regime: ${snap.when_signals.movement_regime} (${snap.when_signals.expected_window}, urgency ${(snap.when_signals.movement_urgency_score * 100).toFixed(0)}%)`
    );
  }

  const cryptoCouncil = params.marketIntel?.crypto_council;
  if (cryptoCouncil) {
    lines.push(
      `- Crypto AI Council (${cryptoCouncil.horizon}): ${cryptoCouncil.directional_bias} · ${cryptoCouncil.regime_verdict} (Move Prob ${(cryptoCouncil.significant_move_prob * 100).toFixed(1)}%, Up ${(cryptoCouncil.up_prob * 100).toFixed(1)}%)`
    );
  }
  const stocksCouncil = params.marketIntel?.stocks_council;
  if (stocksCouncil) {
    lines.push(
      `- Stocks AI Council (${stocksCouncil.horizon}): ${stocksCouncil.directional_bias} · ${stocksCouncil.regime_verdict} (Move Prob ${(stocksCouncil.significant_move_prob * 100).toFixed(1)}%, Up ${(stocksCouncil.up_prob * 100).toFixed(1)}%)`
    );
  }

  const cal = params.marketIntel?.calibration_live_only;
  if (cal) {
    lines.push(
      `- Clean LIVE Calibration: resolved=${cal.resolved_count}, open=${cal.open_count}, mean_brier=${cal.mean_brier_score != null ? cal.mean_brier_score.toFixed(4) : 'n/a'}, excluded_non_live=${cal.excluded_non_live_or_unverified_count}`
    );
  }

  const dash = params.dashboard;
  if (dash) {
    const approvedCount = dash.latest_opportunities.filter((o) => o.risk_gate_approved).length;
    lines.push(
      `- Risk Gate: ${dash.risk_gate.kill_switch.active ? 'HALTED (KILL SWITCH)' : 'FAIL-CLOSED ACTIVE'} | Live Trading: ${dash.live_trading_enabled ? 'ENABLED' : 'DISABLED'} | Latest Polymarket Scan: ${approvedCount}/${dash.latest_opportunities.length} approved`
    );
  }

  return lines.join('\n').slice(0, 3500);
}

export function formatGeminiApiError(err: unknown): {
  status: number;
  message: string;
} {
  const raw = err instanceof Error ? err.message : String(err || 'Gemini request failed');
  const redacted = raw
    .replace(/AIza[0-9A-Za-z_-]+/g, '[REDACTED_KEY]')
    .replace(/Bearer\s+\S+/gi, 'Bearer [REDACTED]');

  const lower = redacted.toLowerCase();
  if (lower.includes('gemini_api_key') && lower.includes('not configured')) {
    return {
      status: 503,
      message:
        'GEMINI_API_KEY is not configured on the server. Add or select an API key in Settings > Secrets.',
    };
  }
  if (lower.includes('permission_denied') || lower.includes('api_key_invalid') || lower.includes('403') || lower.includes('api key not valid')) {
    return {
      status: 403,
      message:
        'Gemini API key was rejected or lacks permission. Verify your key in Settings > Secrets.',
    };
  }
  if (lower.includes('resource_exhausted') || lower.includes('429') || lower.includes('quota')) {
    return {
      status: 429,
      message:
        'Gemini API quota limit reached (429 RESOURCE_EXHAUSTED). Wait briefly or use a billing-enabled key in Settings > Secrets.',
    };
  }
  if (lower.includes('not_found') || lower.includes('404')) {
    return {
      status: 404,
      message: `Requested Gemini model was not found: ${redacted}`,
    };
  }
  return {
    status: 502,
    message: `Gemini API request failed: ${redacted}`,
  };
}
