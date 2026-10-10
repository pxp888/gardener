/**
 * Deterministic test harness for gardener.ts — no LLM calls, no pi runtime.
 * Loads the real extension through jiti (the same loader pi uses) and drives its
 * handlers with synthetic session data projected by pi's own buildSessionProjection.
 *
 * Usage: node test/gardener.test.mjs [turns]   # default 1; pass 2 to test the N-turn counter
 */

import assert from "node:assert/strict";
import { createRequire } from "node:module";

const PI_DIR = "/var/home/pxperrine/.npm-global/lib/node_modules/@earendil-works/pi-coding-agent";
const N = Number(process.argv[2] ?? 1);

// The extension reads its config at module load, so it must be set before import.
delete process.env.PI_GARDENER_PROTECTED_TURNS; // pin K to the default 5 regardless of ambient env
process.env.PI_GARDENER_TURNS = String(N);

// No local node_modules needed: point the bare import at the global install,
// mirroring what pi's own extension loader does with its alias map.
const piRequire = createRequire(`${PI_DIR}/package.json`);
const { createJiti } = piRequire("jiti");
const jiti = createJiti(import.meta.url, {
	alias: { "@earendil-works/pi-coding-agent": `${PI_DIR}/dist/index.js` },
});

const mod = await jiti.import(new URL("../gardener.ts", import.meta.url).pathname);
const factory = mod.default;
assert.equal(typeof factory, "function", "extension must export a default factory");

// --- capture registered handlers -------------------------------------------
const handlers = {};
factory({ on: (name, fn) => (handlers[name] = fn) });
assert.equal(typeof handlers.turn_end, "function", "turn_end handler registered");
assert.equal(typeof handlers.session_before_compact, "function", "session_before_compact handler registered");

// --- raw session entry fixtures --------------------------------------------
let counter = 0;
const nid = () => `id-${String(++counter).padStart(3, "0")}`;

/** Build a linear chain of RAW SessionEntry objects (what pi persists / projects). */
function buildRaw(entriesSpec) {
	let parentId = null;
	return entriesSpec.map((spec) => {
		const id = nid();
		const entry = { ...spec, id, parentId };
		parentId = id;
		return entry;
	});
}

const userEntry = (text) => ({ type: "message", message: { role: "user", content: [{ type: "text", text }] } });
const asstHead = (i) => ({
	type: "message",
	message: {
		role: "assistant",
		content: [
			{ type: "toolCall", id: `tc-${i}`, name: "bash", arguments: { command: `echo g${i}` } },
		],
		stopReason: "toolUse",
		usage: { input: 10, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 15 },
	},
});
const toolRes = (i) => ({
	type: "message",
	message: {
		role: "toolResult",
		toolCallId: `tc-${i}`,
		content: [{ type: "text", text: `g${i}-out` }],
		isError: false,
	},
});

/** 8-turn session (one user prompt, eight assistant steps). */
const freshSpec = [userEntry("task")];
for (let i = 1; i <= 8; i++) freshSpec.push(asstHead(i), toolRes(i));

// --- mock extension context --------------------------------------------------
const calls = [];
const ctx = {
	hasUI: false,
	mode: "print",
	cwd: "/tmp",
	ui: { notify() {} },
	signal: undefined,
	model: { id: "test/main-model" },
	sessionManager: {},
	modelRegistry: {
		complete: async (model, context, options) => {
			calls.push({ modelId: model.id, text: context.messages[0].content[0].text, options });
			return {
				role: "assistant",
				content: [{ type: "text", text: "GARDEN-SUMMARY" }],
				usage: { input: 10, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 15 },
				stopReason: "stop",
			};
		},
	},
};

const fireTurnEnd = async (rawEntries, outcome = "completed") => {
	const projection = jitiRequireBuildProjection(rawEntries);
	return handlers.turn_end(
		{ type: "turn_end", turnIndex: 99, message: { role: "assistant" }, toolResults: [], outcome, context: { contextEntries: projection.entries } },
		ctx,
	);
};

// buildSessionProjection is imported by the extension itself; grab it from pi's dist for the fixture side.
const { buildSessionProjection } = await jiti.import(`${PI_DIR}/dist/core/session-manager.js`);
function jitiRequireBuildProjection(rawEntries) {
	return buildSessionProjection(rawEntries);
}

// --- scenarios ---------------------------------------------------------------
console.log(`# running with PI_GARDENER_TURNS=${N} (set before import)`);

if (N === 1) {
	{
		console.log("# A: fresh session, 8 turns -> garden folds T1..T3, keeps last 5 verbatim");
		const raw = buildRaw(freshSpec);
		const ids = raw.map((e) => e.id);
		const result = await fireTurnEnd(raw);
		assert.ok(result?.entries, "turn_end should return boundary entries");
		assert.equal(result.entries.length, 1);
		const draft = result.entries[0];
		assert.equal(draft.type, "compaction");
		// raw layout: [user0, A1, TR1, A2, TR2, A3, TR3, A4, ...] -> cut at index 7 (head of T4)
		assert.equal(draft.firstKeptEntryId, ids[7], "cut must land on the start of turn 4");
		assert.equal(calls.length, 1, "exactly one gardener LLM call");
		const prompt = calls[0].text;
		assert.ok(prompt.includes("You are the Context Gardener"), "uses the gardener prompt");
		assert.ok(prompt.includes("g3-out"), "eligible content includes aged-out tool results");
		assert.ok(!prompt.includes("echo g4") && !prompt.includes("g5"), "protected tail must not be serialized");
		assert.equal(calls[0].modelId, "test/main-model", "gardener uses the main model");
		assert.equal(calls[0].options.cacheRetention, "none");
		assert.ok(typeof calls[0].options.sessionId === "string" && calls[0].options.sessionId.length > 10);
	}

	{
		console.log("# B: iterative pass over a previous compaction merges the old summary");
		const raw = buildRaw([
			userEntry("task"),
			asstHead(1), toolRes(1), asstHead(2), toolRes(2), asstHead(3), toolRes(3),
			{ type: "compaction", summary: "PREV-SUMMARY-MARKER", firstKeptEntryId: null, tokensBefore: 999 },
			asstHead(4), toolRes(4), asstHead(5), toolRes(5), asstHead(6), toolRes(6),
			asstHead(7), toolRes(7), asstHead(8), toolRes(8), asstHead(9), toolRes(9),
		]);
		calls.length = 0;
		const result = await fireTurnEnd(raw);
		assert.ok(result?.entries, "turn_end should return boundary entries");
		const draft = result.entries[0];
		// projection after compaction: [C, A4..TR9] (6 turns visible) -> keep last 5 => cut at head of T5
		const projIdxOfA5 = raw.findIndex((e) => e.message?.content?.some?.((c) => c.id === "tc-5"));
		assert.equal(draft.firstKeptEntryId, raw[projIdxOfA5].id, "cut must land on the start of turn 5");
		const prompt = calls[0]?.text ?? "";
		assert.ok(prompt.includes("PREV-SUMMARY-MARKER"), "previous summary is passed for iterative merge");
		assert.ok(prompt.includes("g4-out"), "newly aged-out turn content is serialized");
		assert.ok(!prompt.includes("echo g6"), "protected tail must not be serialized");
	}

	{
		console.log("# D: nothing has aged out (<= K turns) -> no garden, no LLM call");
		const raw = buildRaw([userEntry("task"), asstHead(1), toolRes(1), asstHead(2), toolRes(2), asstHead(3), toolRes(3)]);
		calls.length = 0;
		const result = await fireTurnEnd(raw);
		assert.equal(result, undefined, "no boundary entries when nothing aged out");
		assert.equal(calls.length, 0);

		console.log("# E: errored turn -> no garden");
		const raw2 = buildRaw(freshSpec);
		calls.length = 0;
		const result2 = await fireTurnEnd(raw2, "error");
		assert.equal(result2, undefined, "errored turns must not trigger gardening");
		assert.equal(calls.length, 0);
	}

	{
		console.log("# F: session_before_compact path (manual /compact) applies the same routine");
		const raw = buildRaw(freshSpec);
		calls.length = 0;
		const result = await handlers.session_before_compact(
			{
				type: "session_before_compact",
				preparation: { tokensBefore: 12345 },
				branchEntries: raw,
				reason: "manual",
				willRetry: false,
				signal: new AbortController().signal,
				customInstructions: "focus on the test objective",
			},
			ctx,
		);
		assert.ok(result?.compaction, "should return custom compaction content");
		assert.equal(result.compaction.summary, "GARDEN-SUMMARY");
		assert.equal(result.compaction.tokensBefore, 12345, "tokensBefore comes from pi's preparation");
		const ids = raw.map((e) => e.id);
		assert.equal(result.compaction.firstKeptEntryId, ids[7]);
		assert.ok(calls[0].text.includes("focus on the test objective"), "custom /compact instructions are honored");

		console.log("# G: session_before_compact with nothing eligible -> defer to pi's default");
		const rawSmall = buildRaw([userEntry("task"), asstHead(1), toolRes(1)]);
		calls.length = 0;
		const result2 = await handlers.session_before_compact(
			{ type: "session_before_compact", preparation: { tokensBefore: 42 }, branchEntries: rawSmall, reason: "manual", willRetry: false, signal: new AbortController().signal },
			ctx,
		);
		assert.equal(result2, undefined, "falls back to built-in compaction");
		assert.equal(calls.length, 0);
	}
	console.log("ALL PASS (n=1)");
} else {
	{
		console.log("# C: N-turn counter — first completed turn does not garden, second one does");
		const raw = buildRaw(freshSpec);
		calls.length = 0;
		const first = await fireTurnEnd(raw);
		assert.equal(first, undefined, "counter below N -> no garden on the first turn");
		assert.equal(calls.length, 0);

		const second = await fireTurnEnd(raw);
		assert.ok(second?.entries, "garden fires once N turns have completed since the last pass");
		assert.equal(calls.length, 1);
	}
	console.log(`ALL PASS (n=${N})`);
}
