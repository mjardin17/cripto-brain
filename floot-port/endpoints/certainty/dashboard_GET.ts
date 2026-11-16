import { requireViewer } from "@floot/viewer";
import { db } from "../../helpers/db";

function safeNumber(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

export async function handle(_request: Request): Promise<Response> {
  requireViewer();

  const [
    scanCounts,
    opportunityCounts,
    auditCounts,
    memoryCounts,
    paperCounts,
    stateRows,
    recentScans,
    recentAudits,
    recentMemories,
    recentOpportunities,
  ] = await Promise.all([
    db.selectFrom("certaintyScans")
      .select((eb) => [eb.fn.countAll<number>().as("total"), eb.fn.sum<number>("marketsScanned").as("marketsScanned"), eb.fn.max("completedAt").as("lastCompletedAt")])
      .executeTakeFirstOrThrow(),
    db.selectFrom("certaintyArbitrageOpportunities")
      .select((eb) => [eb.fn.countAll<number>().as("total"), eb.fn.sum<number>("riskGateApproved").as("approved")])
      .executeTakeFirstOrThrow(),
    db.selectFrom("certaintyRiskGateAudit")
      .select((eb) => [eb.fn.countAll<number>().as("total"), eb.fn.count<number>("auditId").filterWhere("decision", "=", "DENIED").as("denied")])
      .executeTakeFirstOrThrow(),
    db.selectFrom("certaintyAgentMemory")
      .select((eb) => [eb.fn.countAll<number>().as("total"), eb.fn.count<number>("memoryId").filterWhere("trustState", "=", "VERIFIED").as("verified")])
      .executeTakeFirstOrThrow(),
    db.selectFrom("certaintyPaperOrders")
      .select((eb) => [eb.fn.countAll<number>().as("total"), eb.fn.sum<number>("requestedNotionalUsd").as("notional")])
      .executeTakeFirstOrThrow(),
    db.selectFrom("certaintySystemState").select(["key", "value"]).execute(),
    db.selectFrom("certaintyScans").selectAll().orderBy("startedAt", "desc").limit(8).execute(),
    db.selectFrom("certaintyRiskGateAudit").selectAll().orderBy("decidedAt", "desc").limit(12).execute(),
    db.selectFrom("certaintyAgentMemory").selectAll().orderBy("createdAt", "desc").limit(12).execute(),
    db.selectFrom("certaintyArbitrageOpportunities").selectAll().orderBy("evaluatedAt", "desc").limit(12).execute(),
  ]);

  const state = Object.fromEntries(stateRows.map((row) => [row.key, row.value]));
  const killSwitch = state.kill_switch_active;
  const killSwitchActive = killSwitch === true || (typeof killSwitch === "object" && killSwitch !== null && (killSwitch as { active?: boolean }).active === true);
  const marketCapital = safeNumber(state.paper_capital_allocated_usd);

  return Response.json({
    phase: "Floot migration — backend foundation",
    live_trading_enabled: false,
    provenance: "UNAVAILABLE",
    portfolio: {
      provenance: "SIMULATED",
      paper_capital_allocated_usd: marketCapital,
      deployed_exposure_usd: 0,
      daily_simulated_pnl_usd: 0,
      paper_execution_status: "PAPER_ONLY_DISABLED_UNTIL_VALIDATION",
      venue_exposures: [],
      paper_orders_count: safeNumber(paperCounts.total),
      paper_requested_notional_usd: safeNumber(paperCounts.notional),
    },
    risk_gate: {
      fail_closed: true,
      live_trading_enabled: false,
      kill_switch: { active: killSwitchActive },
      total_audits: safeNumber(auditCounts.total),
      denied_audits: safeNumber(auditCounts.denied),
    },
    counts: {
      scans: safeNumber(scanCounts.total),
      markets_scanned: safeNumber(scanCounts.marketsScanned),
      opportunities: safeNumber(opportunityCounts.total),
      risk_approved_opportunities: safeNumber(opportunityCounts.approved),
      memories: safeNumber(memoryCounts.total),
      verified_memories: safeNumber(memoryCounts.verified),
    },
    recent_scans: recentScans,
    recent_audits: recentAudits,
    recent_memories: recentMemories,
    recent_opportunities: recentOpportunities,
    last_scan_at: scanCounts.lastCompletedAt,
    generated_at: new Date().toISOString(),
  });
}
