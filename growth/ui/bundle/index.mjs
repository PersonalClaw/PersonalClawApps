import { createAgentTask as e, createAppApi as t } from "@personalclaw/app-sdk";
import { Button as n, Surface as r } from "@personalclaw/app-sdk/ui";
import * as i from "react";
import { createRoot as a } from "react-dom/client";
//#region src/index.tsx
var { useState: o, useEffect: s, useCallback: c, useMemo: l } = i, u = {
	Consistent: "var(--color-success)",
	Emerging: "var(--color-warning)",
	Single: "var(--color-info)",
	None: "var(--color-danger)"
}, d = {
	chat: {
		glyph: "💬",
		label: "Chat"
	},
	project: {
		glyph: "▦",
		label: "Project"
	},
	task: {
		glyph: "✓",
		label: "Task"
	},
	knowledge: {
		glyph: "📄",
		label: "Knowledge"
	},
	external: {
		glyph: "🔗",
		label: "Link"
	},
	git: {
		glyph: "⎇",
		label: "Commit"
	}
}, f = [
	"overview",
	"artifacts",
	"sources",
	"areas",
	"digest",
	"settings"
], p = {
	overview: "Overview",
	artifacts: "Artifacts",
	sources: "Sources",
	areas: "Growth areas",
	digest: "Digest",
	settings: "Settings"
}, m = "/apps/growth/api";
function h({ ctx: r }) {
	let a = t(r), l = e(r.name), [u, d] = o("overview"), [h, _] = o(null), [v, b] = o([]), [x, w] = o([]), [T, D] = o([]), [k, A] = o(""), j = c(() => {
		a.get(`${m}/readiness`).then(_).catch((e) => A(String(e.message || e))), a.get(`${m}/artifacts`).then((e) => b(e.artifacts)).catch(() => {}), a.get(`${m}/areas`).then((e) => w(e.areas)).catch(() => {}), a.get(`${m}/digests`).then((e) => D(e.digests)).catch(() => {});
	}, []);
	return s(() => {
		j();
	}, [j]), k ? /* @__PURE__ */ i.createElement(F, { tone: "error" }, k) : /* @__PURE__ */ i.createElement("div", { style: { padding: "var(--spacing-2xl)" } }, /* @__PURE__ */ i.createElement(N, {
		title: "Growth Tracker",
		subtitle: "Turn your real work into evidenced growth — mine your chats, projects, tasks and notes into artifacts."
	}), /* @__PURE__ */ i.createElement("div", { style: {
		display: "flex",
		gap: "var(--spacing-s)",
		margin: "var(--spacing-m) 0",
		flexWrap: "wrap"
	} }, f.map((e) => /* @__PURE__ */ i.createElement(n, {
		variant: u === e ? "primary" : "ghost",
		size: "sm",
		ariaPressed: u === e,
		key: e,
		onClick: () => d(e)
	}, p[e]))), u === "overview" && /* @__PURE__ */ i.createElement(g, {
		readiness: h,
		areas: x,
		artifacts: v,
		onJump: d
	}), u === "artifacts" && /* @__PURE__ */ i.createElement(y, {
		api: a,
		agent: l,
		artifacts: v,
		areas: x,
		onChanged: j
	}), u === "sources" && /* @__PURE__ */ i.createElement(S, {
		api: a,
		agent: l,
		artifacts: v,
		onChanged: j,
		onGoArtifacts: () => d("artifacts")
	}), u === "areas" && /* @__PURE__ */ i.createElement(C, {
		api: a,
		areas: x,
		artifacts: v,
		readiness: h,
		onChanged: j
	}), u === "digest" && /* @__PURE__ */ i.createElement(E, {
		api: a,
		agent: l,
		digests: T,
		artifacts: v,
		areas: x,
		onChanged: j
	}), u === "settings" && /* @__PURE__ */ i.createElement(O, {
		api: a,
		onChanged: j
	}));
}
function g({ readiness: e, areas: t, artifacts: n, onJump: r }) {
	if (!e) return /* @__PURE__ */ i.createElement(F, null, "Loading…");
	let a = n.slice(0, 5);
	return /* @__PURE__ */ i.createElement("div", { style: {
		display: "grid",
		gap: "var(--spacing-l)"
	} }, /* @__PURE__ */ i.createElement("div", { style: {
		display: "flex",
		gap: "var(--spacing-l)",
		alignItems: "stretch",
		flexWrap: "wrap"
	} }, /* @__PURE__ */ i.createElement(_, { pct: e.overall_pct }), /* @__PURE__ */ i.createElement("div", { style: {
		flex: 1,
		minWidth: "17.5rem",
		display: "grid",
		gap: "var(--spacing-s)"
	} }, e.dimensions.map((e) => /* @__PURE__ */ i.createElement("div", {
		key: e.dimension,
		style: {
			display: "flex",
			alignItems: "center",
			gap: "var(--spacing-s)"
		}
	}, /* @__PURE__ */ i.createElement("span", {
		"data-type": "body-s",
		style: { flex: 1 }
	}, e.dimension), /* @__PURE__ */ i.createElement("span", {
		"data-type": "caption",
		style: { color: "var(--color-on-surface-low)" }
	}, e.actual, "/", e.threshold), /* @__PURE__ */ i.createElement("span", {
		"data-type": "caption",
		style: {
			padding: "0 var(--spacing-s)",
			height: "1.375rem",
			display: "inline-flex",
			alignItems: "center",
			borderRadius: "var(--radius-pill)",
			color: u[e.status] || "var(--color-on-surface-low)",
			background: `color-mix(in srgb, ${u[e.status] || "var(--color-surface-high)"} 16%, transparent)`
		}
	}, e.status), /* @__PURE__ */ i.createElement("div", { style: {
		width: "4.375rem",
		height: "0.3125rem",
		borderRadius: "var(--radius-xs)",
		background: "var(--color-surface-high)"
	} }, /* @__PURE__ */ i.createElement("div", { style: {
		width: `${e.pct}%`,
		height: "100%",
		borderRadius: "var(--radius-xs)",
		background: "var(--color-primary)"
	} })))))), e.gaps.length > 0 && /* @__PURE__ */ i.createElement(F, null, "No evidence yet for ", /* @__PURE__ */ i.createElement("b", null, e.gaps.join(", ")), ". ", /* @__PURE__ */ i.createElement(I, { onClick: () => r("sources") }, "Mine your work"), " or ", /* @__PURE__ */ i.createElement(I, { onClick: () => r("artifacts") }, "add an artifact"), "."), t.length > 0 && /* @__PURE__ */ i.createElement(P, { title: "Growth areas" }, /* @__PURE__ */ i.createElement("div", { style: {
		display: "grid",
		gap: "var(--spacing-s)"
	} }, t.map((e) => /* @__PURE__ */ i.createElement(M, {
		key: e.id,
		style: {
			display: "flex",
			alignItems: "center",
			gap: "var(--spacing-s)"
		}
	}, /* @__PURE__ */ i.createElement("span", {
		"data-type": "body-s",
		style: {
			flex: 1,
			fontVariationSettings: "\"wght\" 600"
		}
	}, e.name, e.dimension ? /* @__PURE__ */ i.createElement("span", { style: {
		color: "var(--color-on-surface-low)",
		fontVariationSettings: "\"wght\" 400"
	} }, " · ", e.dimension) : null), /* @__PURE__ */ i.createElement("span", {
		"data-type": "caption",
		style: { color: "var(--color-on-surface-var)" }
	}, e.artifact_count, " artifact", e.artifact_count === 1 ? "" : "s"))))), /* @__PURE__ */ i.createElement(P, { title: `Recent artifacts (${n.length})` }, a.length === 0 ? /* @__PURE__ */ i.createElement(F, null, "No artifacts yet. ", /* @__PURE__ */ i.createElement(I, { onClick: () => r("sources") }, "Mine your PClaw work"), " to draft your first, or add one manually.") : a.map((e) => /* @__PURE__ */ i.createElement(v, {
		key: e.id,
		a: e
	}))));
}
function _({ pct: e }) {
	let t = 2 * Math.PI * 42;
	return /* @__PURE__ */ i.createElement(M, { style: {
		width: "9.375rem",
		display: "grid",
		placeItems: "center",
		gap: "var(--spacing-xs)"
	} }, /* @__PURE__ */ i.createElement("svg", {
		width: "110",
		height: "110",
		viewBox: "0 0 110 110"
	}, /* @__PURE__ */ i.createElement("circle", {
		cx: "55",
		cy: "55",
		r: 42,
		fill: "none",
		stroke: "var(--color-surface-high)",
		strokeWidth: "10"
	}), /* @__PURE__ */ i.createElement("circle", {
		cx: "55",
		cy: "55",
		r: 42,
		fill: "none",
		stroke: "var(--color-primary)",
		strokeWidth: "10",
		strokeLinecap: "round",
		strokeDasharray: t,
		strokeDashoffset: t * (1 - e / 100),
		transform: "rotate(-90 55 55)"
	}), /* @__PURE__ */ i.createElement("text", {
		x: "55",
		y: "55",
		textAnchor: "middle",
		dominantBaseline: "central",
		fontSize: "22",
		fill: "var(--color-on-surface)",
		fontWeight: "600"
	}, e, "%")), /* @__PURE__ */ i.createElement("span", {
		"data-type": "caption",
		style: {
			color: "var(--color-on-surface-var)",
			textAlign: "center"
		}
	}, "dimensions covered"));
}
function v({ a: e, onClick: t, onEdit: r, onDelete: a }) {
	return /* @__PURE__ */ i.createElement(M, {
		style: { cursor: t ? "pointer" : "default" },
		onClick: t,
		testId: "artifact"
	}, /* @__PURE__ */ i.createElement("div", { style: {
		display: "flex",
		gap: "var(--spacing-s)",
		alignItems: "center"
	} }, /* @__PURE__ */ i.createElement("span", {
		"data-type": "body-m",
		style: {
			flex: 1,
			fontVariationSettings: "\"wght\" 600"
		}
	}, e.title), e.sourced && /* @__PURE__ */ i.createElement("span", {
		"data-type": "caption",
		style: { color: "var(--color-success)" }
	}, "✓ sourced"), r && /* @__PURE__ */ i.createElement(n, {
		variant: "ghost",
		size: "xs",
		onClick: (e) => {
			e.stopPropagation(), r();
		}
	}, "edit"), a && /* @__PURE__ */ i.createElement(n, {
		variant: "ghost",
		size: "xs",
		onClick: (e) => {
			e.stopPropagation(), a();
		}
	}, "delete")), /* @__PURE__ */ i.createElement("div", {
		"data-type": "caption",
		style: {
			color: "var(--color-on-surface-var)",
			marginTop: "var(--spacing-xs)"
		}
	}, e.date, " · ", e.dimensions.join(", ") || "unclassified"), e.evidence.length > 0 && /* @__PURE__ */ i.createElement("div", { style: {
		display: "flex",
		gap: "var(--spacing-s)",
		flexWrap: "wrap",
		marginTop: "var(--spacing-s)"
	} }, e.evidence.map((e, t) => /* @__PURE__ */ i.createElement("span", {
		key: t,
		"data-type": "caption",
		style: {
			padding: "var(--spacing-xs) var(--spacing-s)",
			borderRadius: "var(--radius-pill)",
			background: "var(--color-surface-high)"
		},
		title: e.ref
	}, d[e.kind]?.glyph || "🔗", " ", e.label || e.ref))));
}
function y({ api: e, agent: t, artifacts: r, areas: a, onChanged: s }) {
	let [c, l] = o(!1), [u, d] = o(null);
	return /* @__PURE__ */ i.createElement("div", null, /* @__PURE__ */ i.createElement("div", { style: {
		display: "flex",
		justifyContent: "space-between",
		alignItems: "center"
	} }, /* @__PURE__ */ i.createElement("p", {
		"data-type": "body-s",
		style: {
			color: "var(--color-on-surface-low)",
			margin: 0
		}
	}, "Evidenced pieces of growth. Compose from a PClaw source or freeform."), !c && !u && /* @__PURE__ */ i.createElement(n, {
		variant: "primary",
		size: "md",
		onClick: () => l(!0)
	}, "New artifact")), c && /* @__PURE__ */ i.createElement(b, {
		api: e,
		agent: t,
		areas: a,
		onDone: () => {
			l(!1), s();
		},
		onCancel: () => l(!1)
	}), u && /* @__PURE__ */ i.createElement(b, {
		api: e,
		agent: t,
		areas: a,
		editTarget: u,
		onDone: () => {
			d(null), s();
		},
		onCancel: () => d(null)
	}), /* @__PURE__ */ i.createElement(P, { title: `Artifacts (${r.length})` }, r.length === 0 ? /* @__PURE__ */ i.createElement(F, null, "No artifacts yet.") : r.map((t) => /* @__PURE__ */ i.createElement(v, {
		key: t.id,
		a: t,
		onEdit: () => d(t),
		onDelete: () => e.del(`${m}/artifacts/${t.id}`).then(s)
	}))));
}
function b({ api: e, agent: t, areas: r, onDone: a, onCancel: s, seed: c, editTarget: l }) {
	let [u, f] = o(l?.title || c?.title || ""), [p, h] = o(l?.situation || ""), [g, _] = o(l?.behavior || ""), [v, y] = o(l?.impact || ""), [b, S] = o(l?.evidence || c?.evidence || []), [C, w] = o(l?.area_id || ""), [T, E] = o(!1), [D, O] = o(""), [j, N] = o(""), P = async () => {
		if (!D) {
			O("Drafting from evidence…"), N("");
			try {
				let e = b.map((e) => `${d[e.kind]?.label || e.kind}: ${e.label} (${e.ref})`).join("; "), n = c?.sourceText ? `\n\nSource content:\n${c.sourceText.slice(0, 2e3)}` : "", r = `Draft a work-contribution in SBI form (Situation, Behavior, Impact) from this evidence. Reply ONLY as JSON with keys title, situation, behavior, impact. Be concrete + factual; do not invent metrics not present. Evidence: ${e || u}.${n}`, i = await t.run(r, { maxTurns: 3 }), a = {};
				try {
					a = JSON.parse((i.result || "").replace(/^[^{]*/, "").replace(/[^}]*$/, ""));
				} catch {}
				a.title && f(a.title), a.situation && h(a.situation), a.behavior && _(a.behavior), a.impact && y(a.impact), O("");
			} catch (e) {
				N(String(e.message || e)), O("");
			}
		}
	}, I = async () => {
		if (!u.trim() || D) {
			N("Title is required.");
			return;
		}
		O("Saving…");
		try {
			let t = {
				title: u.trim(),
				situation: p,
				behavior: g,
				impact: v,
				evidence: b,
				area_id: C,
				source: b.length ? "sourced" : "manual"
			};
			l ? await e.patch(`${m}/artifacts/${l.id}`, t) : await e.post(`${m}/artifacts`, t), a();
		} catch (e) {
			N(String(e.message || e)), O("");
		}
	};
	return /* @__PURE__ */ i.createElement(M, { style: {
		marginTop: "var(--spacing-m)",
		display: "grid",
		gap: "var(--spacing-s)"
	} }, /* @__PURE__ */ i.createElement("input", {
		value: u,
		"aria-label": "Artifact title",
		onChange: (e) => f(e.target.value),
		placeholder: "What did you accomplish?",
		"data-type": "body-m",
		style: k,
		"data-testid": "composer-title"
	}), /* @__PURE__ */ i.createElement("div", { style: {
		display: "flex",
		gap: "var(--spacing-s)",
		alignItems: "center",
		flexWrap: "wrap"
	} }, /* @__PURE__ */ i.createElement(n, {
		variant: "secondary",
		size: "sm",
		onClick: () => E(!0)
	}, "+ Link evidence"), b.map((e, t) => /* @__PURE__ */ i.createElement("span", {
		key: t,
		"data-type": "caption",
		style: {
			padding: "var(--spacing-xs) var(--spacing-s)",
			borderRadius: "var(--radius-pill)",
			background: "var(--color-surface-high)",
			display: "inline-flex",
			gap: "var(--spacing-s)",
			alignItems: "center"
		}
	}, d[e.kind]?.glyph || "🔗", " ", e.label || e.ref, /* @__PURE__ */ i.createElement(n, {
		variant: "ghost",
		size: "xs",
		onClick: () => S(b.filter((e, n) => n !== t)),
		ariaLabel: "Remove evidence"
	}, "×"))), b.length > 0 && /* @__PURE__ */ i.createElement(n, {
		variant: "secondary",
		size: "sm",
		onClick: P,
		disabled: !!D
	}, "✨ Draft from evidence")), /* @__PURE__ */ i.createElement("textarea", {
		value: p,
		"aria-label": "Situation",
		onChange: (e) => h(e.target.value),
		placeholder: "Situation — the context",
		rows: 2,
		"data-type": "body-m",
		style: {
			...k,
			resize: "vertical",
			fontFamily: "inherit"
		}
	}), /* @__PURE__ */ i.createElement("textarea", {
		value: g,
		"aria-label": "Behavior",
		onChange: (e) => _(e.target.value),
		placeholder: "Behavior — what you did",
		rows: 2,
		"data-type": "body-m",
		style: {
			...k,
			resize: "vertical",
			fontFamily: "inherit"
		}
	}), /* @__PURE__ */ i.createElement("textarea", {
		value: v,
		"aria-label": "Impact",
		onChange: (e) => y(e.target.value),
		placeholder: "Impact — the outcome",
		rows: 2,
		"data-type": "body-m",
		style: {
			...k,
			resize: "vertical",
			fontFamily: "inherit"
		}
	}), r.length > 0 && /* @__PURE__ */ i.createElement("select", {
		value: C,
		"aria-label": "Growth area",
		onChange: (e) => w(e.target.value),
		style: A
	}, /* @__PURE__ */ i.createElement("option", { value: "" }, "No growth area"), r.map((e) => /* @__PURE__ */ i.createElement("option", {
		key: e.id,
		value: e.id
	}, e.name))), /* @__PURE__ */ i.createElement("div", { style: {
		display: "flex",
		gap: "var(--spacing-s)",
		alignItems: "center"
	} }, /* @__PURE__ */ i.createElement(n, {
		variant: "primary",
		size: "md",
		onClick: I,
		disabled: !!D || !u.trim(),
		disabledReason: D ? "Saving…" : "Give the artifact a title first"
	}, D || (l ? "Update artifact" : "Save artifact")), /* @__PURE__ */ i.createElement(n, {
		variant: "ghost",
		size: "xs",
		onClick: s
	}, "Cancel")), j && /* @__PURE__ */ i.createElement(F, { tone: "error" }, j), T && /* @__PURE__ */ i.createElement(x, {
		api: e,
		onPick: (e) => {
			S([...b, e]), E(!1);
		},
		onClose: () => E(!1)
	}));
}
function x({ api: e, onPick: t, onClose: r }) {
	let [a, c] = o("project"), [l, u] = o([]), [f, p] = o(!1), [m, h] = o("");
	return s(() => {
		if (a === "external") {
			u([]);
			return;
		}
		p(!0), (async () => a === "project" ? ((await e.get("/api/projects")).projects || []).map((e) => ({
			kind: "project",
			ref: e.id,
			label: e.name
		})) : a === "task" ? ((await e.get("/api/tasks?status=done&limit=30")).tasks || []).map((e) => ({
			kind: "task",
			ref: e.id,
			label: e.title
		})) : ((await e.get("/api/knowledge/items?limit=30")).items || []).map((e) => ({
			kind: "knowledge",
			ref: e.id,
			label: e.title
		})))().then(u).catch(() => u([])).finally(() => p(!1));
	}, [a]), /* @__PURE__ */ i.createElement(M, { style: {
		background: "var(--color-surface-high)",
		display: "grid",
		gap: "var(--spacing-s)"
	} }, /* @__PURE__ */ i.createElement("div", { style: {
		display: "flex",
		gap: "var(--spacing-s)",
		alignItems: "center"
	} }, /* @__PURE__ */ i.createElement("span", {
		"data-type": "caption",
		style: { color: "var(--color-on-surface-var)" }
	}, "Link evidence from:"), [
		"project",
		"task",
		"knowledge",
		"external"
	].map((e) => /* @__PURE__ */ i.createElement(n, {
		variant: a === e ? "primary" : "ghost",
		size: "sm",
		ariaPressed: a === e,
		key: e,
		onClick: () => c(e)
	}, d[e]?.label || e)), /* @__PURE__ */ i.createElement("span", { style: { flex: 1 } }), /* @__PURE__ */ i.createElement(n, {
		variant: "ghost",
		size: "xs",
		onClick: r
	}, "close")), a === "external" ? /* @__PURE__ */ i.createElement("div", { style: {
		display: "flex",
		gap: "var(--spacing-s)"
	} }, /* @__PURE__ */ i.createElement("input", {
		value: m,
		"aria-label": "External URL or reference",
		onChange: (e) => h(e.target.value),
		placeholder: "https://… or a PR / doc reference",
		"data-type": "body-m",
		style: k
	}), /* @__PURE__ */ i.createElement(n, {
		variant: "primary",
		size: "md",
		onClick: () => {
			m.trim() && t({
				kind: "external",
				ref: m.trim(),
				label: m.trim().slice(0, 40)
			});
		}
	}, "Add")) : f ? /* @__PURE__ */ i.createElement("div", {
		"data-type": "caption",
		style: { color: "var(--color-on-surface-low)" }
	}, "Loading ", a, "s…") : l.length === 0 ? /* @__PURE__ */ i.createElement("div", {
		"data-type": "caption",
		style: { color: "var(--color-on-surface-low)" }
	}, "No ", a, "s found.") : /* @__PURE__ */ i.createElement("div", { style: {
		display: "grid",
		gap: "var(--spacing-xs)",
		maxHeight: "13.75rem",
		overflow: "auto"
	} }, l.map((e, n) => /* @__PURE__ */ i.createElement(M, {
		key: n,
		onClick: () => t(e),
		testId: "evidence-option",
		"data-type": "body-s",
		style: { padding: "var(--spacing-s)" }
	}, d[e.kind]?.glyph || "🔗", " ", e.label))));
}
function S({ api: e, agent: t, artifacts: r, onChanged: a, onGoArtifacts: u }) {
	let [f, p] = o(null), [h, g] = o(/* @__PURE__ */ new Set()), [_, v] = o(null), [y, x] = o(""), S = l(() => new Set(r.flatMap((e) => e.evidence.map((e) => `${e.kind}:${e.ref}`))), [r]), C = c(async () => {
		p(null), x("");
		try {
			let [t, n, r] = await Promise.all([
				e.get(`${m}/dismissed`).then((e) => e.dismissed).catch(() => []),
				e.get("/api/projects").then((e) => e.projects || []).catch(() => []),
				e.get("/api/tasks?status=done&limit=25").then((e) => e.tasks || []).catch(() => [])
			]);
			g(new Set(t));
			let i = [];
			for (let e of n) e.name !== "Personal" && e.name !== "Repeatable" && i.push({
				kind: "project",
				ref: e.id,
				title: e.name,
				subtitle: `Project · ${e.status}`,
				text: `Project "${e.name}" (status ${e.status}) — an autonomous body of work you drove.`
			});
			for (let e of r) i.push({
				kind: "task",
				ref: e.id,
				title: e.title,
				subtitle: "Completed task",
				text: `Completed task: ${e.title}`
			});
			p(i);
		} catch (e) {
			x(String(e.message || e));
		}
	}, []);
	s(() => {
		C();
	}, [C]);
	let w = async (t) => {
		g(/* @__PURE__ */ new Set([...h, t]));
		try {
			await e.post(`${m}/dismissed`, { ref: t });
		} catch {}
	}, T = (f || []).filter((e) => {
		let t = `${e.kind}:${e.ref}`;
		return !h.has(t) && !S.has(t);
	});
	return _ ? /* @__PURE__ */ i.createElement("div", null, /* @__PURE__ */ i.createElement(n, {
		variant: "ghost",
		size: "xs",
		onClick: () => v(null)
	}, "← Back to sources"), /* @__PURE__ */ i.createElement(b, {
		api: e,
		agent: t,
		areas: [],
		seed: _,
		onDone: () => {
			v(null), a(), u();
		},
		onCancel: () => v(null)
	})) : /* @__PURE__ */ i.createElement("div", null, /* @__PURE__ */ i.createElement("div", { style: {
		display: "flex",
		justifyContent: "space-between",
		alignItems: "center"
	} }, /* @__PURE__ */ i.createElement("p", {
		"data-type": "body-s",
		style: {
			color: "var(--color-on-surface-low)",
			margin: 0
		}
	}, "Your real PClaw work — completed projects + tasks — as candidate artifacts. Draft one, or dismiss."), /* @__PURE__ */ i.createElement(n, {
		variant: "secondary",
		size: "sm",
		onClick: C
	}, "Refresh")), y && /* @__PURE__ */ i.createElement(F, { tone: "error" }, y), /* @__PURE__ */ i.createElement(P, { title: f === null ? "Mining your work…" : `Candidates (${T.length})` }, f === null ? /* @__PURE__ */ i.createElement(F, null, "Reading your projects + tasks…") : T.length === 0 ? /* @__PURE__ */ i.createElement(F, null, "Nothing new to surface. Completed projects + tasks appear here as you do the work.") : T.map((e) => /* @__PURE__ */ i.createElement(M, {
		key: `${e.kind}:${e.ref}`,
		testId: "source-candidate"
	}, /* @__PURE__ */ i.createElement("div", { style: {
		display: "flex",
		gap: "var(--spacing-s)",
		alignItems: "center"
	} }, /* @__PURE__ */ i.createElement("span", {
		"data-type": "body-s",
		style: {
			flex: 1,
			fontVariationSettings: "\"wght\" 600"
		}
	}, d[e.kind]?.glyph || "🔗", " ", e.title), /* @__PURE__ */ i.createElement(n, {
		variant: "primary",
		size: "md",
		onClick: () => v({
			title: e.title,
			evidence: [{
				kind: e.kind,
				ref: e.ref,
				label: e.title
			}],
			sourceText: e.text
		})
	}, "Draft artifact"), /* @__PURE__ */ i.createElement(n, {
		variant: "ghost",
		size: "xs",
		onClick: () => w(`${e.kind}:${e.ref}`)
	}, "dismiss")), /* @__PURE__ */ i.createElement("div", {
		"data-type": "caption",
		style: {
			color: "var(--color-on-surface-low)",
			marginTop: "var(--spacing-xs)"
		}
	}, e.subtitle)))));
}
function C({ api: e, areas: t, artifacts: r, readiness: a, onChanged: s }) {
	let [c, l] = o(""), [u, d] = o(""), [f, p] = o(""), [h, g] = o(null), [_, v] = o(""), [y, b] = o(""), [x, S] = o(""), [C, w] = o(""), T = a?.dimensions.map((e) => e.dimension) || [], E = async () => {
		c.trim() && (await e.post(`${m}/areas`, {
			name: c.trim(),
			target: u.trim(),
			dimension: f
		}), l(""), d(""), p(""), s());
	}, D = (e) => {
		g(e.id), v(e.name), b(e.target), S(e.dimension), w(e.status);
	}, O = async () => {
		h && _.trim() && (await e.patch(`${m}/areas/${h}`, {
			name: _.trim(),
			target: y.trim(),
			dimension: x,
			status: C
		}), g(null), s());
	};
	return /* @__PURE__ */ i.createElement("div", null, /* @__PURE__ */ i.createElement(P, { title: "Define a growth area" }, /* @__PURE__ */ i.createElement("div", { style: {
		display: "grid",
		gap: "var(--spacing-s)"
	} }, /* @__PURE__ */ i.createElement("input", {
		value: c,
		"aria-label": "Growth area name",
		onChange: (e) => l(e.target.value),
		placeholder: "e.g. Cross-team influence",
		"data-type": "body-m",
		style: k,
		"data-testid": "area-name"
	}), /* @__PURE__ */ i.createElement("input", {
		value: u,
		"aria-label": "Target",
		onChange: (e) => d(e.target.value),
		placeholder: "Target — what does success look like?",
		"data-type": "body-m",
		style: k
	}), /* @__PURE__ */ i.createElement("div", { style: {
		display: "flex",
		gap: "var(--spacing-s)"
	} }, /* @__PURE__ */ i.createElement("select", {
		value: f,
		"aria-label": "Rubric dimension",
		onChange: (e) => p(e.target.value),
		style: A
	}, /* @__PURE__ */ i.createElement("option", { value: "" }, "Any dimension"), T.map((e) => /* @__PURE__ */ i.createElement("option", {
		key: e,
		value: e
	}, e))), /* @__PURE__ */ i.createElement(n, {
		variant: "primary",
		size: "md",
		onClick: E,
		disabled: !c.trim(),
		disabledReason: "Name the growth area first"
	}, "Add area")))), /* @__PURE__ */ i.createElement(P, { title: `Growth areas (${t.length})` }, t.length === 0 ? /* @__PURE__ */ i.createElement(F, null, "No growth areas yet. Define what you're deliberately working toward.") : t.map((t) => {
		let a = r.filter((e) => e.area_id === t.id);
		return h === t.id ? /* @__PURE__ */ i.createElement(M, {
			key: t.id,
			testId: "area-editing"
		}, /* @__PURE__ */ i.createElement("div", { style: {
			display: "grid",
			gap: "var(--spacing-s)"
		} }, /* @__PURE__ */ i.createElement("input", {
			value: _,
			"aria-label": "Area name",
			onChange: (e) => v(e.target.value),
			"data-type": "body-m",
			style: k
		}), /* @__PURE__ */ i.createElement("input", {
			value: y,
			"aria-label": "Target",
			onChange: (e) => b(e.target.value),
			placeholder: "Target",
			"data-type": "body-m",
			style: k
		}), /* @__PURE__ */ i.createElement("div", { style: {
			display: "flex",
			gap: "var(--spacing-s)"
		} }, /* @__PURE__ */ i.createElement("select", {
			value: x,
			"aria-label": "Dimension",
			onChange: (e) => S(e.target.value),
			style: A
		}, /* @__PURE__ */ i.createElement("option", { value: "" }, "Any dimension"), T.map((e) => /* @__PURE__ */ i.createElement("option", {
			key: e,
			value: e
		}, e))), /* @__PURE__ */ i.createElement("select", {
			value: C,
			"aria-label": "Status",
			onChange: (e) => w(e.target.value),
			style: A
		}, /* @__PURE__ */ i.createElement("option", { value: "active" }, "Active"), /* @__PURE__ */ i.createElement("option", { value: "completed" }, "Completed"), /* @__PURE__ */ i.createElement("option", { value: "paused" }, "Paused"))), /* @__PURE__ */ i.createElement("div", { style: {
			display: "flex",
			gap: "var(--spacing-s)"
		} }, /* @__PURE__ */ i.createElement(n, {
			variant: "primary",
			size: "md",
			onClick: O,
			disabled: !_.trim(),
			disabledReason: "The name cannot be empty"
		}, "Save"), /* @__PURE__ */ i.createElement(n, {
			variant: "ghost",
			size: "xs",
			onClick: () => g(null)
		}, "Cancel")))) : /* @__PURE__ */ i.createElement(M, {
			key: t.id,
			testId: "area"
		}, /* @__PURE__ */ i.createElement("div", { style: {
			display: "flex",
			gap: "var(--spacing-s)",
			alignItems: "center"
		} }, /* @__PURE__ */ i.createElement("span", {
			"data-type": "body-m",
			style: {
				flex: 1,
				fontVariationSettings: "\"wght\" 600"
			}
		}, t.name, t.dimension ? /* @__PURE__ */ i.createElement("span", {
			"data-type": "caption",
			style: { color: "var(--color-on-surface-low)" }
		}, " · ", t.dimension) : null), /* @__PURE__ */ i.createElement("span", {
			"data-type": "caption",
			style: { color: "var(--color-on-surface-var)" }
		}, a.length, " artifact", a.length === 1 ? "" : "s"), /* @__PURE__ */ i.createElement(n, {
			variant: "ghost",
			size: "xs",
			onClick: () => D(t)
		}, "edit"), /* @__PURE__ */ i.createElement(n, {
			variant: "ghost",
			size: "xs",
			onClick: () => e.del(`${m}/areas/${t.id}`).then(s)
		}, "delete")), t.target && /* @__PURE__ */ i.createElement("div", {
			"data-type": "body-s",
			style: {
				color: "var(--color-on-surface-var)",
				marginTop: "var(--spacing-xs)"
			}
		}, "🎯 ", t.target), t.status && t.status !== "active" && /* @__PURE__ */ i.createElement("div", {
			"data-type": "caption",
			style: {
				color: "var(--color-on-surface-low)",
				marginTop: "var(--spacing-xs)"
			}
		}, "Status: ", t.status), a.length === 0 && /* @__PURE__ */ i.createElement("div", {
			"data-type": "caption",
			style: {
				color: "var(--color-on-surface-low)",
				marginTop: "var(--spacing-xs)",
				fontStyle: "italic"
			}
		}, "No evidence yet — link an artifact to show progress."), a.map((e) => /* @__PURE__ */ i.createElement("div", {
			key: e.id,
			"data-type": "caption",
			style: {
				color: "var(--color-on-surface-var)",
				marginTop: "var(--spacing-xs)"
			}
		}, "• ", e.title)));
	})));
}
function w() {
	let e = /* @__PURE__ */ new Date();
	return `${e.getFullYear()}-Q${Math.floor(e.getMonth() / 3) + 1}`;
}
function T(e) {
	let t = e.match(/^```[a-zA-Z]*\n([\s\S]*?)\n?```$/);
	return t ? t[1].trim() : e;
}
function E({ api: e, agent: t, digests: r, artifacts: a, areas: s, onChanged: c }) {
	let [l, u] = o(w()), [d, f] = o(!1), [p, h] = o(""), g = async () => {
		if (d) return;
		let n = a.filter((e) => e.period === l), r = n.length ? n : a;
		if (!r.length) {
			h("No artifacts to summarize.");
			return;
		}
		f(!0), h("Generating…");
		try {
			let n = Object.fromEntries(s.map((e) => [e.id, e.name])), i = r.map((e) => ({
				title: e.title,
				situation: e.situation,
				behavior: e.behavior,
				impact: e.impact,
				dimensions: e.dimensions,
				date: e.date,
				area: n[e.area_id] || "",
				evidence: e.evidence.map((e) => e.label || e.ref)
			})), a = `Write a concise, professional growth digest ("brag doc") in Markdown for the period "${l}". Base it ONLY on these evidenced artifacts (JSON) — do NOT invent details or metrics. Group by growth area or dimension, lead each item with measurable impact, and cite the evidence in parentheses. Keep it tight + factual. Return ONLY the raw Markdown document — no code fences, no preamble or commentary. Artifacts: ${JSON.stringify(i)}`, o = T(((await t.run(a, { maxTurns: 3 })).result || "").trim());
			if (!o) throw Error("empty digest");
			await e.post(`${m}/digests`, {
				period: l,
				content_md: o
			}), h(""), c();
		} catch (e) {
			h(String(e.message || e));
		} finally {
			f(!1);
		}
	}, _ = (e) => {
		try {
			navigator.clipboard?.writeText(e);
		} catch {}
	}, v = async (t) => {
		try {
			await e.post("/api/knowledge/items", {
				type: "note",
				title: `Growth digest — ${t.period || "all"}`,
				content: t.content_md
			}), h("Exported to Knowledge.");
		} catch (e) {
			h(String(e.message || e));
		}
	};
	return /* @__PURE__ */ i.createElement("div", null, /* @__PURE__ */ i.createElement(P, { title: "Generate a digest" }, /* @__PURE__ */ i.createElement("div", { style: {
		display: "flex",
		gap: "var(--spacing-s)"
	} }, /* @__PURE__ */ i.createElement("input", {
		value: l,
		"aria-label": "Digest period",
		onChange: (e) => u(e.target.value),
		placeholder: "Period e.g. 2026-Q3",
		"data-type": "body-m",
		style: k,
		"data-testid": "digest-period"
	}), /* @__PURE__ */ i.createElement(n, {
		variant: "primary",
		size: "md",
		onClick: g,
		disabled: d
	}, d ? p || "Generating…" : "Generate")), /* @__PURE__ */ i.createElement("p", {
		"data-type": "caption",
		style: {
			color: "var(--color-on-surface-low)",
			marginTop: "var(--spacing-xs)"
		}
	}, p && !d ? p : "Summarizes your evidenced artifacts into a shareable accomplishment doc that cites its sources.")), /* @__PURE__ */ i.createElement(P, { title: `Digests (${r.length})` }, r.length === 0 ? /* @__PURE__ */ i.createElement(F, null, "No digests yet. Pick a period and generate one.") : r.map((t) => /* @__PURE__ */ i.createElement(M, {
		key: t.id,
		testId: "digest"
	}, /* @__PURE__ */ i.createElement("div", { style: {
		display: "flex",
		gap: "var(--spacing-s)",
		alignItems: "center"
	} }, /* @__PURE__ */ i.createElement("span", {
		"data-type": "body-m",
		style: {
			flex: 1,
			fontVariationSettings: "\"wght\" 600"
		}
	}, t.period || "—"), /* @__PURE__ */ i.createElement("span", {
		"data-type": "caption",
		style: { color: "var(--color-on-surface-low)" }
	}, (t.created_at || "").slice(0, 10)), /* @__PURE__ */ i.createElement(n, {
		variant: "ghost",
		size: "xs",
		onClick: () => _(t.content_md)
	}, "copy"), /* @__PURE__ */ i.createElement(n, {
		variant: "ghost",
		size: "xs",
		onClick: () => v(t)
	}, "export → Knowledge"), /* @__PURE__ */ i.createElement(n, {
		variant: "ghost",
		size: "xs",
		onClick: () => e.del(`${m}/digests/${t.id}`).then(c)
	}, "delete")), /* @__PURE__ */ i.createElement("div", {
		style: {
			marginTop: "var(--spacing-s)",
			padding: "var(--spacing-m)",
			borderRadius: "var(--radius-sm)",
			background: "var(--color-surface-high)",
			whiteSpace: "pre-wrap"
		},
		"data-type": "body-s"
	}, t.content_md)))));
}
function D({ value: e, onChange: t }) {
	let n = (e || []).join(", "), [r, a] = o(n), [c, l] = o(!1);
	return s(() => {
		c || a(n);
	}, [n, c]), /* @__PURE__ */ i.createElement("input", {
		value: r,
		"aria-label": "Requirement keywords, comma-separated",
		onChange: (e) => {
			a(e.target.value), t(e.target.value.split(",").map((e) => e.trim()).filter(Boolean));
		},
		onFocus: () => l(!0),
		onBlur: () => l(!1),
		placeholder: "keywords, comma-separated",
		"data-type": "body-m",
		style: k
	});
}
function O({ api: e, onChanged: t }) {
	let [r, a] = o(null), [c, l] = o(!1), [u, d] = o("");
	if (s(() => {
		e.get(`${m}/rubric`).then((e) => {
			a(e.rubric), l(e.is_override);
		}).catch((e) => d(String(e.message || e)));
	}, []), !r) return /* @__PURE__ */ i.createElement(F, null, "Loading rubric…");
	let f = async () => {
		try {
			await e.put(`${m}/rubric`, r), l(!0), d("Rubric saved."), t();
		} catch (e) {
			d(String(e.message || e));
		}
	}, p = async () => {
		try {
			let n = await e.post(`${m}/rubric/reset`);
			a(n.rubric), l(!1), d("Reset to default."), t();
		} catch (e) {
			d(String(e.message || e));
		}
	}, h = (e, t) => a((n) => n && {
		...n,
		dimensions: n.dimensions.map((n, r) => r === e ? t : n)
	}), g = () => a((e) => e && {
		...e,
		dimensions: [...e.dimensions, "New dimension"]
	}), _ = (e) => a((t) => t && {
		...t,
		dimensions: t.dimensions.filter((t, n) => n !== e)
	}), v = (e, t) => a((n) => n && {
		...n,
		requirements: n.requirements.map((n, r) => r === e ? {
			...n,
			...t
		} : n)
	}), y = () => a((e) => e && {
		...e,
		requirements: [...e.requirements, {
			code: `R${e.requirements.length + 1}`,
			dim: e.dimensions[0] || "",
			short: "",
			threshold: 1,
			keywords: []
		}]
	}), b = (e) => a((t) => t && {
		...t,
		requirements: t.requirements.filter((t, n) => n !== e)
	});
	return /* @__PURE__ */ i.createElement("div", null, /* @__PURE__ */ i.createElement(P, { title: "Growth rubric (scoring lens)" }, /* @__PURE__ */ i.createElement("p", {
		"data-type": "caption",
		style: {
			color: "var(--color-on-surface-low)",
			margin: "0 0 var(--spacing-s)"
		}
	}, c ? "Using your custom rubric." : "Using the built-in default.", " Dimensions + keyword requirements drive classification + readiness scoring — they don't limit what you can log."), /* @__PURE__ */ i.createElement("label", {
		"data-type": "caption",
		style: j
	}, "Label"), /* @__PURE__ */ i.createElement("input", {
		value: r.label,
		"aria-label": "Rubric label",
		onChange: (e) => a((t) => t && {
			...t,
			label: e.target.value
		}),
		"data-type": "body-m",
		style: {
			...k,
			width: "100%",
			marginBottom: "var(--spacing-m)"
		},
		"data-testid": "rubric-label"
	}), /* @__PURE__ */ i.createElement("label", {
		"data-type": "caption",
		style: j
	}, "Dimensions"), /* @__PURE__ */ i.createElement("div", { style: {
		display: "grid",
		gap: "var(--spacing-s)",
		marginBottom: "var(--spacing-m)"
	} }, r.dimensions.map((e, t) => /* @__PURE__ */ i.createElement("div", {
		key: t,
		style: {
			display: "flex",
			gap: "var(--spacing-s)"
		}
	}, /* @__PURE__ */ i.createElement("input", {
		value: e,
		"aria-label": `Dimension ${t + 1}`,
		onChange: (e) => h(t, e.target.value),
		"data-type": "body-m",
		style: {
			...k,
			flex: 1
		}
	}), /* @__PURE__ */ i.createElement(n, {
		variant: "secondary",
		size: "sm",
		onClick: () => _(t),
		ariaLabel: "Remove dimension"
	}, "×"))), /* @__PURE__ */ i.createElement(n, {
		variant: "secondary",
		size: "sm",
		onClick: g
	}, "+ Add dimension")), /* @__PURE__ */ i.createElement("label", {
		"data-type": "caption",
		style: j
	}, "Requirements"), /* @__PURE__ */ i.createElement("div", { style: {
		display: "grid",
		gap: "var(--spacing-s)",
		marginBottom: "var(--spacing-m)"
	} }, r.requirements.map((e, t) => /* @__PURE__ */ i.createElement(M, {
		key: t,
		style: {
			marginBottom: 0,
			display: "grid",
			gap: "var(--spacing-s)"
		}
	}, /* @__PURE__ */ i.createElement("div", { style: {
		display: "flex",
		gap: "var(--spacing-s)",
		alignItems: "center"
	} }, /* @__PURE__ */ i.createElement("input", {
		value: e.code,
		"aria-label": "Requirement code",
		onChange: (e) => v(t, { code: e.target.value }),
		placeholder: "code",
		"data-type": "body-m",
		style: {
			...k,
			width: "5rem"
		}
	}), /* @__PURE__ */ i.createElement("select", {
		value: e.dim,
		"aria-label": "Requirement dimension",
		onChange: (e) => v(t, { dim: e.target.value }),
		style: {
			...A,
			flex: 1
		}
	}, r.dimensions.map((e) => /* @__PURE__ */ i.createElement("option", {
		key: e,
		value: e
	}, e))), /* @__PURE__ */ i.createElement("span", {
		"data-type": "caption",
		style: { color: "var(--color-on-surface-low)" }
	}, "threshold"), /* @__PURE__ */ i.createElement("input", {
		type: "number",
		min: 1,
		"aria-label": "Requirement threshold",
		value: e.threshold,
		onChange: (e) => v(t, { threshold: Math.max(1, Number(e.target.value) || 1) }),
		"data-type": "body-m",
		style: {
			...k,
			width: "3.75rem"
		}
	}), /* @__PURE__ */ i.createElement(n, {
		variant: "secondary",
		size: "sm",
		onClick: () => b(t),
		ariaLabel: "Remove requirement"
	}, "×")), /* @__PURE__ */ i.createElement("input", {
		value: e.short || "",
		"aria-label": "Requirement short label",
		onChange: (e) => v(t, { short: e.target.value }),
		placeholder: "short label",
		"data-type": "body-m",
		style: k
	}), /* @__PURE__ */ i.createElement(D, {
		value: e.keywords || [],
		onChange: (e) => v(t, { keywords: e })
	}))), /* @__PURE__ */ i.createElement(n, {
		variant: "secondary",
		size: "sm",
		onClick: y
	}, "+ Add requirement")), /* @__PURE__ */ i.createElement("div", { style: {
		display: "flex",
		gap: "var(--spacing-s)"
	} }, /* @__PURE__ */ i.createElement(n, {
		variant: "primary",
		size: "md",
		onClick: f
	}, "Save rubric"), /* @__PURE__ */ i.createElement(n, {
		variant: "ghost",
		size: "xs",
		onClick: p
	}, "Reset to default"))), u && /* @__PURE__ */ i.createElement(F, null, u));
}
var k = {
	flex: 1,
	padding: "var(--spacing-s) var(--spacing-m)",
	borderRadius: "var(--radius-md)",
	border: "none",
	background: "var(--color-surface-high)",
	color: "var(--color-on-surface)",
	outline: "none"
}, A = {
	...k,
	appearance: "none",
	paddingRight: "1.875rem"
}, j = {
	color: "var(--color-on-surface-low)",
	marginBottom: "var(--spacing-xs)",
	display: "block"
};
function M({ children: e, style: t, onClick: n, testId: a, tone: o }) {
	let s = {
		padding: "var(--spacing-l)",
		width: "100%",
		textAlign: "left",
		...t
	};
	return /* @__PURE__ */ i.createElement("div", { style: { marginBottom: "var(--spacing-s)" } }, /* @__PURE__ */ i.createElement(r, {
		tone: o ?? "container",
		radius: "lg"
	}, n ? /* @__PURE__ */ i.createElement("button", {
		type: "button",
		onClick: n,
		"data-testid": a,
		style: {
			...s,
			background: "none",
			border: "none",
			color: "inherit",
			font: "inherit",
			cursor: "pointer"
		}
	}, e) : /* @__PURE__ */ i.createElement("div", {
		style: s,
		"data-testid": a
	}, e)));
}
function N({ title: e, subtitle: t }) {
	return /* @__PURE__ */ i.createElement("div", null, /* @__PURE__ */ i.createElement("h2", {
		"data-type": "title-l",
		style: {
			margin: 0,
			color: "var(--color-on-surface)"
		}
	}, e), t && /* @__PURE__ */ i.createElement("p", {
		"data-type": "body-s",
		style: {
			color: "var(--color-on-surface-low)",
			margin: "var(--spacing-s) 0 0"
		}
	}, t));
}
function P({ title: e, children: t }) {
	return /* @__PURE__ */ i.createElement("section", { style: { margin: "var(--spacing-2xl) 0" } }, /* @__PURE__ */ i.createElement("h3", {
		"data-type": "label-m",
		style: {
			margin: "0 0 var(--spacing-s)",
			color: "var(--color-on-surface)"
		}
	}, e), t);
}
function F({ children: e, tone: t }) {
	return /* @__PURE__ */ i.createElement("div", {
		"data-type": "body-s",
		style: {
			padding: "var(--spacing-m)",
			borderRadius: "var(--radius-md)",
			border: "1px solid var(--color-outline-variant)",
			background: "var(--color-surface-high)",
			color: t === "error" ? "var(--color-danger)" : "var(--color-on-surface-low)"
		}
	}, e);
}
function I({ onClick: e, children: t }) {
	return /* @__PURE__ */ i.createElement("button", {
		type: "button",
		onClick: e,
		style: {
			background: "none",
			border: "none",
			padding: 0,
			font: "inherit",
			color: "var(--color-primary)",
			cursor: "pointer",
			textDecoration: "underline"
		}
	}, t);
}
function L(e, t) {
	let n = a(e);
	return n.render(/* @__PURE__ */ i.createElement(h, { ctx: t })), () => n.unmount();
}
//#endregion
export { L as mount };
