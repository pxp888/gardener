/**
 * Gardener — turn-based context compaction for pi
 *
 * After every N completed turns (default 1), a "gardener" pass summarizes all
 * conversation older than the last K turns (default 5) and replaces it with a
 * compact, factual state note. The protected tail is kept verbatim, so the main
 * agent can continue without re-reading stale tool output, failed searches stay
 * recorded as conclusions ("checked records.db — only expense summaries"), and
 * boilerplate that can be regenerated never clogs context again.
 *
 * How it works:
 *   - A "turn" is one assistant step (a model response plus its tool results),
 *     the same unit pi counts in turn_end events.
 *   - Each pass only summarizes the turns that just aged out of the K-turn
 *     window, merged with the previous summary — so per-pass cost stays small
 *     even when gardening every turn.
 *   - The gardener uses the session's current model (same as main), called via
 *     ctx.modelRegistry.complete() with a fresh sessionId and no cache writes.
 *   - The result is committed as a canonical compaction entry at the turn_end
 *     boundary, so pi replays it (plus the protected tail) for every later request.
 *   - pi's own compactions (context threshold, overflow recovery, manual /compact)
 *     are intercepted and routed through the same gardener routine instead of the
 *     default 20k-token keep.
 *
 * Load:      auto-discovered from .pi/extensions/gardener/index.ts (project must be trusted)
 * Global:    cp -r .pi/extensions/gardener ~/.pi/agent/extensions/gardener
 * Tune:      PI_GARDENER_TURNS=3 PI_GARDENER_PROTECTED_TURNS=8 pi ...
 *            (PI_GARDENER_TURNS=0 disables the periodic pass; /compact still gardens)
 */

import { randomUUID } from "node:crypto";
import type { ExtensionAPI, ExtensionContext, ModelRegistry, ProjectedSessionEntry } from "@earendil-works/pi-coding-agent";
import { buildSessionProjection, convertToLlm, estimateTokens, serializeConversation } from "@earendil-works/pi-coding-agent";

type ContextMessage = ProjectedSessionEntry["messages"][number];
type GardenUsage = NonNullable<Awaited<ReturnType<ModelRegistry["complete"]>>["usage"]>;

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

function envInt(name: string, fallback: number, min: number): number {
	const value = Number(process.env[name]);
	return Number.isInteger(value) && value >= min ? value : fallback;
}

const CONFIG = {
	/** Garden after every N completed turns. 0 disables the periodic pass (manual /compact still works). */
	turns: envInt("PI_GARDENER_TURNS", 1, 0),
	/** Keep the last K turns verbatim; everything older is folded into the summary. */
	protectedTurns: envInt("PI_GARDENER_PROTECTED_TURNS", 5, 1),
	/** Skip a pass when the eligible (aging-out) content is below this many tokens. */
	minEligibleTokens: 0,
	/** Max output tokens for the gardener's replacement text. */
	maxSummaryTokens: 4096,
};

// ---------------------------------------------------------------------------
// Gardener prompt (verbatim per spec; operational notes appended at call time)
// ---------------------------------------------------------------------------

const GARDENER_PROMPT = `You are the Context Gardener for a coding agent. Your responsibility is to reduce the agent's conversation context while preserving everything necessary to continue its current objective correctly.

Review the eligible historical conversation and produce a concise replacement that allows the main agent to resume work without repeating investigations, losing important discoveries, or undoing successful changes.

Prioritize preserving:

1. The current objective, requirements, acceptance criteria, and constraints.

2. Current implementation state, including relevant files, functions, interfaces, and successful changes.

3. Important discoveries, including failed searches, negative findings, environmental limitations, and rejected approaches.

4. Decisions already made and their rationale.

5. Outstanding work, unresolved questions, blockers, and the next useful steps.

6. Relevant user instructions and preferences.


Remove verbose tool outputs, irrelevant exploration, redundant explanations, superseded plans, and boilerplate that can safely be regenerated.

Do not treat a failed investigation as worthless. Preserve its conclusion whenever it prevents repeated work or constrains future decisions. Prefer a short factual statement over a transcript of the investigation.

Do not invent facts, infer that an operation succeeded when only an attempt is recorded, or claim that files contain particular code unless the available context supports that claim.

The newest protected turns are outside your editing scope. Preserve their contents exactly.

Produce a compact, factual context replacement. Optimize for the main agent's ability to continue working, not for a comprehensive historical narrative. When uncertain whether information is essential, preserve it in a concise form rather than deleting it.`;

// ---------------------------------------------------------------------------
// Planning: what ages out, where the protected tail starts
// ---------------------------------------------------------------------------

interface GardenPlan {
	/** Messages that will be folded into the summary (older than the protected tail). */
	eligibleMessages: ContextMessage[];
	/** Number of assistant turns among eligibleMessages. */
	eligibleTurns: number;
	/** Estimated token size of eligibleMessages. */
	estimatedTokens: number;
	/** Summary from the previous compaction, for iterative merge (if any). */
	previousSummary?: string;
	/** Id of the first entry to keep verbatim (start of the protected tail). */
	firstKeptEntryId: string;
}

/**
 * Compute a garden plan from the canonical session projection.
 * Returns null when nothing has aged out beyond the protected window, or the
 * eligible content is below CONFIG.minEligibleTokens.
 */
function planGarden(projected: ProjectedSessionEntry[]): GardenPlan | null {
	// Locate the newest compaction that contributes a summary message; everything
	// before it is already summarized and must not be recounted or re-serialized.
	let boundary = 0;
	let previousSummary: string | undefined;
	for (let i = 0; i < projected.length; i++) {
		const entry = projected[i];
		if (entry.sourceEntry.type === "compaction" && entry.messages.length > 0) {
			boundary = i + 1;
			previousSummary = entry.sourceEntry.summary;
		}
	}

	// Walk backwards counting assistant steps ("turns"); cut at the start of the
	// K-th most recent one. That boundary is always a user or assistant message,
	// never inside tool results, so it is a valid compaction cut point.
	let headsSeen = 0;
	let cutIndex = -1;
	for (let i = projected.length - 1; i >= boundary; i--) {
		if (projected[i].messages.some((m) => m.role === "assistant")) {
			headsSeen++;
			if (headsSeen === CONFIG.protectedTurns) {
				cutIndex = i;
				break;
			}
		}
	}
	if (cutIndex <= boundary) return null;

	const eligibleMessages: ContextMessage[] = [];
	let eligibleTurns = 0;
	for (let i = boundary; i < cutIndex; i++) {
		const entry = projected[i];
		if (entry.messages.some((m) => m.role === "assistant")) eligibleTurns++;
		for (const message of entry.messages) {
			if (message.role !== "system") eligibleMessages.push(message);
		}
	}
	if (eligibleMessages.length === 0 || eligibleTurns === 0) return null;

	let estimatedTokens = 0;
	for (const message of eligibleMessages) estimatedTokens += estimateTokens(message);
	if (estimatedTokens < CONFIG.minEligibleTokens) return null;

	return {
		eligibleMessages,
		eligibleTurns,
		estimatedTokens,
		previousSummary,
		firstKeptEntryId: projected[cutIndex].sourceEntry.id,
	};
}

// ---------------------------------------------------------------------------
// Gardening: one model call with the session's current model
// ---------------------------------------------------------------------------

function buildGardenerRequest(plan: GardenPlan, customInstructions?: string): string {
	const conversationText = serializeConversation(convertToLlm(plan.eligibleMessages));

	const parts: string[] = [
		GARDENER_PROMPT,
		[
			"Operational notes:",
			"- Your replacement will be shown to the main agent as its session state, in place of the summarized history.",
			"- Output only the replacement context: no preamble, no closing remarks, no markdown code fences.",
		].join("\n"),
	];

	if (plan.previousSummary) {
		parts.push(
			"The conversation below is newer than this previous state note. Merge it into an updated state; do not repeat verbatim what is already covered unless it changed.\n<previous-context>\n" +
				plan.previousSummary +
				"\n</previous-context>",
		);
	}

	const focus = customInstructions?.trim();
	if (focus) parts.push(`Additional focus requested by the user: ${focus}`);

	parts.push(`<conversation>\n${conversationText}\n</conversation>`);
	return parts.join("\n\n");
}

async function runGarden(
	ctx: ExtensionContext,
	plan: GardenPlan,
	signal?: AbortSignal,
	customInstructions?: string,
): Promise<{ summary: string; usage?: GardenUsage } | null> {
	const model = ctx.model;
	if (!model) return null;

	try {
		const response = await ctx.modelRegistry.complete(
			model,
			{
				messages: [
					{
						role: "user" as const,
						content: [{ type: "text" as const, text: buildGardenerRequest(plan, customInstructions) }],
						timestamp: Date.now(),
					},
				],
			},
			{ maxTokens: CONFIG.maxSummaryTokens, cacheRetention: "none", sessionId: randomUUID(), signal },
		);

		const summary = response.content
			.filter((c): c is { type: "text"; text: string } => c.type === "text")
			.map((c) => c.text)
			.join("\n")
			.trim();

		if (!summary) return null;
		if (response.stopReason === "length") {
			ctx.ui?.notify("gardener: summary hit the token cap and was discarded", "warning");
			return null;
		}
		if (response.stopReason === "error" || response.stopReason === "aborted") {
			throw new Error(response.errorMessage ?? `gardener stopped: ${response.stopReason}`);
		}
		return { summary, usage: response.usage };
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		if (!signal?.aborted) ctx.ui?.notify(`gardener failed: ${message}`, "error");
		return null;
	}
}

// ---------------------------------------------------------------------------
// Extension wiring
// ---------------------------------------------------------------------------

export default function gardener(pi: ExtensionAPI) {
	let turnsSinceGarden = 0;
	let inFlight = false;

	const notify = (ctx: ExtensionContext, text: string, kind: "info" | "warning" | "error" = "info") => {
		if (ctx.hasUI) ctx.ui.notify(text, kind);
	};

	pi.on("session_start", () => {
		turnsSinceGarden = 0;
		inFlight = false;
	});

	// Primary path: garden at the turn_end boundary after every N completed turns.
	pi.on("turn_end", async (event, ctx) => {
		if (CONFIG.turns === 0 || event.outcome !== "completed" || inFlight) return undefined;

		turnsSinceGarden++;
		if (turnsSinceGarden < CONFIG.turns) return undefined;

		const plan = planGarden(event.context.contextEntries);
		if (!plan) return undefined; // nothing has aged out yet

		inFlight = true;
		try {
			notify(ctx, `🌿 gardener: folding ${plan.eligibleTurns} turn(s), keeping last ${CONFIG.protectedTurns}`);
			const result = await runGarden(ctx, plan, ctx.signal);
			turnsSinceGarden = 0; // on failure too — avoid hammering a broken model every turn
			if (!result) return undefined;

			return {
				entries: [
					{
						type: "compaction" as const,
						summary: result.summary,
						firstKeptEntryId: plan.firstKeptEntryId,
						usage: result.usage,
					},
				],
			};
		} finally {
			inFlight = false;
		}
	});

	// Fallback path: when pi's own compaction fires (threshold, overflow recovery,
	// manual /compact), apply the same gardener routine instead of the default
	// token-based keep. Returning undefined lets pi fall back to its built-in pass.
	pi.on("session_before_compact", async (event, ctx) => {
		if (inFlight) return undefined;

		const plan = planGarden(buildSessionProjection(event.branchEntries).entries);
		if (!plan) return undefined;

		inFlight = true;
		try {
			notify(ctx, `🌿 gardener: folding ${plan.eligibleTurns} turn(s), keeping last ${CONFIG.protectedTurns}`);
			const result = await runGarden(ctx, plan, event.signal, event.customInstructions);
			if (!result) return undefined;

			return {
				compaction: {
					summary: result.summary,
					firstKeptEntryId: plan.firstKeptEntryId,
					tokensBefore: event.preparation.tokensBefore,
					usage: result.usage,
				},
			};
		} finally {
			inFlight = false;
		}
	});
}
