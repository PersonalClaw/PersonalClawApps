/** Growth Tracker UI — imperative mount(el, ctx) bundle (ESM, host-provided React).
 *
 * Reimagined from motive: turn your REAL work (chat sessions, projects/loops you ran, tasks you
 * closed, notes you wrote — read from core) into evidenced growth ARTIFACTS, alongside your own
 * notes. The rubric is a scoring LENS, not the product.
 *
 * Screens:
 *   Overview  — readiness ring + growth-area progress + recent artifacts (the dashboard).
 *   Artifacts — the evidenced-work stream; compose new (pick PClaw evidence → agent drafts SBI).
 *   Sources   — mine PClaw activity (projects/tasks/knowledge) into candidate artifacts (accept/dismiss).
 *   Areas     — deliberate growth goals; linked artifacts show progress + gap nudges.
 *   Digest    — generate a shareable accomplishment doc citing artifacts; export to Knowledge.
 *   Settings  — the customizable rubric (dimensions + keyword requirements).
 *
 * Core reads (projects/tasks/knowledge) + agent-run go through the app SDK from the browser.
 */
import { createAppApi, createAgentTask, type AppContext } from '@personalclaw/app-sdk'
import * as React from 'react'
import { createRoot, type Root } from 'react-dom/client'

const { useState, useEffect, useCallback, useMemo } = React

interface Evidence { kind: string; ref: string; label: string }
interface Artifact { id: string; title: string; date: string; period?: string; dimensions: string[]; evidence: Evidence[]; area_id: string; sourced: boolean; source: string; situation: string; behavior: string; impact: string; narrative: string }
interface Area { id: string; name: string; description: string; target: string; dimension: string; status: string; artifact_count: number }
interface ReadinessDim { dimension: string; actual: number; threshold: number; status: string; pct: number }
interface Readiness { dimensions: ReadinessDim[]; overall_pct: number; gaps: string[]; singles: string[] }
interface RubricReq { code: string; dim: string; short?: string; text?: string; threshold: number; keywords: string[] }
interface RubricDoc { label: string; dimensions: string[]; requirements: RubricReq[] }
interface Digest { id: string; period: string; content_md: string; created_at: string }

// Rubric-status palette → host semantic tokens (theme-aware, no raw hex).
const STATUS_COLOR: Record<string, string> = {
  Consistent: 'var(--color-success)', Emerging: 'var(--color-warning)',
  Single: 'var(--color-info)', None: 'var(--color-danger)',
}
// Evidence-source kind → icon glyph + accent (kept token-driven; glyphs are unicode, not raw color).
const KIND_META: Record<string, { glyph: string; label: string }> = {
  chat: { glyph: '💬', label: 'Chat' }, project: { glyph: '▦', label: 'Project' },
  task: { glyph: '✓', label: 'Task' }, knowledge: { glyph: '📄', label: 'Knowledge' },
  external: { glyph: '🔗', label: 'Link' }, git: { glyph: '⎇', label: 'Commit' },
}

const TABS = ['overview', 'artifacts', 'sources', 'areas', 'digest', 'settings'] as const
type Tab = (typeof TABS)[number]
const TAB_LABEL: Record<Tab, string> = {
  overview: 'Overview', artifacts: 'Artifacts', sources: 'Sources', areas: 'Growth areas',
  digest: 'Digest', settings: 'Settings',
}

const A = '/apps/growth/api'

function App({ ctx }: { ctx: AppContext }) {
  const api = createAppApi(ctx)
  const agent = createAgentTask(ctx.name)
  const [tab, setTab] = useState<Tab>('overview')
  const [readiness, setReadiness] = useState<Readiness | null>(null)
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [areas, setAreas] = useState<Area[]>([])
  const [digests, setDigests] = useState<Digest[]>([])
  const [err, setErr] = useState('')

  const load = useCallback(() => {
    api.get<Readiness>(`${A}/readiness`).then(setReadiness).catch((e) => setErr(String(e.message || e)))
    api.get<{ artifacts: Artifact[] }>(`${A}/artifacts`).then((d) => setArtifacts(d.artifacts)).catch(() => {})
    api.get<{ areas: Area[] }>(`${A}/areas`).then((d) => setAreas(d.areas)).catch(() => {})
    api.get<{ digests: Digest[] }>(`${A}/digests`).then((d) => setDigests(d.digests)).catch(() => {})
  }, [])
  useEffect(() => { load() }, [load])

  if (err) return <Notice tone="error">{err}</Notice>

  return (
    <div style={{ maxWidth: 'var(--content-width, 940px)', margin: '0 auto', padding: 24 }}>
      <Header title="Growth Tracker" subtitle="Turn your real work into evidenced growth — mine your chats, projects, tasks and notes into artifacts." />
      <div style={{ display: 'flex', gap: 6, margin: '14px 0', flexWrap: 'wrap' }}>
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)} data-testid={`tab-${t}`}
            style={{ ...tabStyle, ...(tab === t ? tabActive : {}) }}>{TAB_LABEL[t]}</button>
        ))}
      </div>

      {tab === 'overview' && <Overview readiness={readiness} areas={areas} artifacts={artifacts} onJump={setTab} />}
      {tab === 'artifacts' && <Artifacts api={api} agent={agent} artifacts={artifacts} areas={areas} onChanged={load} />}
      {tab === 'sources' && <Sources api={api} agent={agent} artifacts={artifacts} onChanged={load} onGoArtifacts={() => setTab('artifacts')} />}
      {tab === 'areas' && <Areas api={api} areas={areas} artifacts={artifacts} readiness={readiness} onChanged={load} />}
      {tab === 'digest' && <DigestTab api={api} agent={agent} digests={digests} artifacts={artifacts} areas={areas} onChanged={load} />}
      {tab === 'settings' && <Settings api={api} onChanged={load} />}
    </div>
  )
}

// ── Overview: readiness ring + area progress + recent artifacts ──────────────────────────
function Overview({ readiness, areas, artifacts, onJump }: {
  readiness: Readiness | null; areas: Area[]; artifacts: Artifact[]; onJump: (t: Tab) => void
}) {
  if (!readiness) return <Notice>Loading…</Notice>
  const recent = artifacts.slice(0, 5)
  return (
    <div style={{ display: 'grid', gap: 18 }}>
      <div style={{ display: 'flex', gap: 18, alignItems: 'stretch', flexWrap: 'wrap' }}>
        <Ring pct={readiness.overall_pct} />
        <div style={{ flex: 1, minWidth: 280, display: 'grid', gap: 6 }}>
          {readiness.dimensions.map((d) => (
            <div key={d.dimension} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ flex: 1, fontSize: 13 }}>{d.dimension}</span>
              <span style={{ fontSize: 11, opacity: 0.6 }}>{d.actual}/{d.threshold}</span>
              <span style={{ padding: '0 8px', height: 22, display: 'inline-flex', alignItems: 'center', borderRadius: 'var(--radius-pill, 9999px)', fontSize: '0.72rem', color: STATUS_COLOR[d.status] || 'var(--color-on-surface-low)', background: `color-mix(in srgb, ${STATUS_COLOR[d.status] || 'var(--color-surface-high)'} 16%, transparent)` }}>{d.status}</span>
              <div style={{ width: 70, height: 5, borderRadius: 3, background: 'var(--color-surface-high)' }}>
                <div style={{ width: `${d.pct}%`, height: '100%', borderRadius: 3, background: 'var(--color-primary)' }} />
              </div>
            </div>
          ))}
        </div>
      </div>

      {readiness.gaps.length > 0 && (
        <Notice>No evidence yet for <b>{readiness.gaps.join(', ')}</b>. <Link onClick={() => onJump('sources')}>Mine your work</Link> or <Link onClick={() => onJump('artifacts')}>add an artifact</Link>.</Notice>
      )}

      {areas.length > 0 && (
        <Section title="Growth areas">
          <div style={{ display: 'grid', gap: 8 }}>
            {areas.map((a) => (
              <div key={a.id} style={{ ...cardStyle, cursor: 'default', display: 'flex', alignItems: 'center', gap: 10 }}>
                <span style={{ flex: 1, fontWeight: 600, fontSize: 13.5 }}>{a.name}{a.dimension ? <span style={{ opacity: 0.5, fontWeight: 400 }}> · {a.dimension}</span> : null}</span>
                <span style={{ fontSize: 11.5, opacity: 0.7 }}>{a.artifact_count} artifact{a.artifact_count === 1 ? '' : 's'}</span>
              </div>
            ))}
          </div>
        </Section>
      )}

      <Section title={`Recent artifacts (${artifacts.length})`}>
        {recent.length === 0
          ? <Notice>No artifacts yet. <Link onClick={() => onJump('sources')}>Mine your PClaw work</Link> to draft your first, or add one manually.</Notice>
          : recent.map((a) => <ArtifactRow key={a.id} a={a} />)}
      </Section>
    </div>
  )
}

function Ring({ pct }: { pct: number }) {
  const r = 42, circ = 2 * Math.PI * r
  return (
    <div style={{ ...cardStyle, cursor: 'default', width: 150, display: 'grid', placeItems: 'center', gap: 4 }}>
      <svg width="110" height="110" viewBox="0 0 110 110">
        <circle cx="55" cy="55" r={r} fill="none" stroke="var(--color-surface-high)" strokeWidth="10" />
        <circle cx="55" cy="55" r={r} fill="none" stroke="var(--color-primary)" strokeWidth="10" strokeLinecap="round"
          strokeDasharray={circ} strokeDashoffset={circ * (1 - pct / 100)} transform="rotate(-90 55 55)" />
        <text x="55" y="55" textAnchor="middle" dominantBaseline="central" fontSize="22" fill="var(--color-on-surface)" fontWeight="600">{pct}%</text>
      </svg>
      <span style={{ fontSize: 11.5, opacity: 0.7, textAlign: 'center' }}>dimensions covered</span>
    </div>
  )
}

function ArtifactRow({ a, onClick, onEdit, onDelete }: { a: Artifact; onClick?: () => void; onEdit?: () => void; onDelete?: () => void }) {
  return (
    <div style={{ ...cardStyle, cursor: onClick ? 'pointer' : 'default' }} onClick={onClick} data-testid="artifact">
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <span style={{ flex: 1, fontWeight: 600, fontSize: 14 }}>{a.title}</span>
        {a.sourced && <span style={{ fontSize: 11, color: 'var(--color-success)' }}>✓ sourced</span>}
        {onEdit && <button onClick={(e) => { e.stopPropagation(); onEdit() }} style={linkBtn} data-testid="artifact-edit">edit</button>}
        {onDelete && <button onClick={(e) => { e.stopPropagation(); onDelete() }} style={linkBtn} data-testid="artifact-delete">delete</button>}
      </div>
      <div style={{ fontSize: 12, opacity: 0.7, marginTop: 2 }}>{a.date} · {a.dimensions.join(', ') || 'unclassified'}</div>
      {a.evidence.length > 0 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 6 }}>
          {a.evidence.map((e, i) => (
            <span key={i} style={{ fontSize: 10.5, padding: '2px 7px', borderRadius: 999, background: 'var(--color-surface-high)', opacity: 0.9 }}
              title={e.ref}>{(KIND_META[e.kind]?.glyph || '🔗')} {e.label || e.ref}</span>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Artifacts: the stream + the rich composer ──────────────────────────────────────────
function Artifacts({ api, agent, artifacts, areas, onChanged }: {
  api: ReturnType<typeof createAppApi>; agent: ReturnType<typeof createAgentTask>; artifacts: Artifact[]; areas: Area[]; onChanged: () => void
}) {
  const [composing, setComposing] = useState(false)
  const [editing, setEditing] = useState<Artifact | null>(null)
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <p style={{ fontSize: 12.5, opacity: 0.6, margin: 0 }}>Evidenced pieces of growth. Compose from a PClaw source or freeform.</p>
        {!composing && !editing && <button onClick={() => setComposing(true)} style={primaryBtn} data-testid="new-artifact">New artifact</button>}
      </div>
      {composing && <ArtifactComposer api={api} agent={agent} areas={areas} onDone={() => { setComposing(false); onChanged() }} onCancel={() => setComposing(false)} />}
      {editing && <ArtifactComposer api={api} agent={agent} areas={areas} editTarget={editing}
        onDone={() => { setEditing(null); onChanged() }} onCancel={() => setEditing(null)} />}
      <Section title={`Artifacts (${artifacts.length})`}>
        {artifacts.length === 0 ? <Notice>No artifacts yet.</Notice>
          : artifacts.map((a) => <ArtifactRow key={a.id} a={a}
            onEdit={() => setEditing(a)}
            onDelete={() => api.del(`${A}/artifacts/${a.id}`).then(onChanged)} />)}
      </Section>
    </div>
  )
}

/** The rich composer: pick evidence from PClaw (project/task/knowledge/chat) → agent drafts SBI
 *  narrative from that evidence → you edit → dimensions auto-classify on save. */
function ArtifactComposer({ api, agent, areas, onDone, onCancel, seed, editTarget }: {
  api: ReturnType<typeof createAppApi>; agent: ReturnType<typeof createAgentTask>; areas: Area[]
  onDone: () => void; onCancel: () => void
  seed?: { title?: string; evidence?: Evidence[]; sourceText?: string }
  editTarget?: Artifact
}) {
  const [title, setTitle] = useState(editTarget?.title || seed?.title || '')
  const [situation, setSituation] = useState(editTarget?.situation || '')
  const [behavior, setBehavior] = useState(editTarget?.behavior || '')
  const [impact, setImpact] = useState(editTarget?.impact || '')
  const [evidence, setEvidence] = useState<Evidence[]>(editTarget?.evidence || seed?.evidence || [])
  const [areaId, setAreaId] = useState(editTarget?.area_id || '')
  const [picking, setPicking] = useState(false)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')

  const draftFromEvidence = async () => {
    if (busy) return
    setBusy('Drafting from evidence…'); setErr('')
    try {
      const ev = evidence.map((e) => `${KIND_META[e.kind]?.label || e.kind}: ${e.label} (${e.ref})`).join('; ')
      const ctx = seed?.sourceText ? `\n\nSource content:\n${seed.sourceText.slice(0, 2000)}` : ''
      const task = `Draft a work-contribution in SBI form (Situation, Behavior, Impact) from this evidence. Reply ONLY as JSON with keys title, situation, behavior, impact. Be concrete + factual; do not invent metrics not present. Evidence: ${ev || title}.${ctx}`
      const res = await agent.run(task, { maxTurns: 3 })
      let p: Record<string, string> = {}
      try { p = JSON.parse((res.result || '').replace(/^[^{]*/, '').replace(/[^}]*$/, '')) } catch { /* keep fields */ }
      if (p.title) setTitle(p.title)
      if (p.situation) setSituation(p.situation)
      if (p.behavior) setBehavior(p.behavior)
      if (p.impact) setImpact(p.impact)
      setBusy('')
    } catch (e) { setErr(String((e as Error).message || e)); setBusy('') }
  }

  const save = async () => {
    if (!title.trim() || busy) { setErr('Title is required.'); return }
    setBusy('Saving…')
    try {
      const body = { title: title.trim(), situation, behavior, impact, evidence, area_id: areaId, source: evidence.length ? 'sourced' : 'manual' }
      if (editTarget) {
        await api.patch(`${A}/artifacts/${editTarget.id}`, body)
      } else {
        await api.post(`${A}/artifacts`, body)
      }
      onDone()
    } catch (e) { setErr(String((e as Error).message || e)); setBusy('') }
  }

  return (
    <div style={{ ...cardStyle, cursor: 'default', marginTop: 12, display: 'grid', gap: 8 }}>
      <input value={title} aria-label="Artifact title" onChange={(e) => setTitle(e.target.value)} placeholder="What did you accomplish?" style={inputStyle} data-testid="composer-title" />
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <button onClick={() => setPicking(true)} style={smallBtn} data-testid="pick-evidence">+ Link evidence</button>
        {evidence.map((e, i) => (
          <span key={i} style={{ fontSize: 11, padding: '2px 8px', borderRadius: 999, background: 'var(--color-surface-high)', display: 'inline-flex', gap: 6, alignItems: 'center' }}>
            {(KIND_META[e.kind]?.glyph || '🔗')} {e.label || e.ref}
            <button onClick={() => setEvidence(evidence.filter((_, j) => j !== i))} style={{ ...linkBtn, fontSize: 11 }} aria-label="Remove evidence">×</button>
          </span>
        ))}
        {evidence.length > 0 && <button onClick={draftFromEvidence} disabled={!!busy} style={smallBtn} data-testid="draft-from-evidence">✨ Draft from evidence</button>}
      </div>
      <textarea value={situation} aria-label="Situation" onChange={(e) => setSituation(e.target.value)} placeholder="Situation — the context" rows={2} style={{ ...inputStyle, resize: 'vertical', fontFamily: 'inherit' }} />
      <textarea value={behavior} aria-label="Behavior" onChange={(e) => setBehavior(e.target.value)} placeholder="Behavior — what you did" rows={2} style={{ ...inputStyle, resize: 'vertical', fontFamily: 'inherit' }} />
      <textarea value={impact} aria-label="Impact" onChange={(e) => setImpact(e.target.value)} placeholder="Impact — the outcome" rows={2} style={{ ...inputStyle, resize: 'vertical', fontFamily: 'inherit' }} />
      {areas.length > 0 && (
        <select value={areaId} aria-label="Growth area" onChange={(e) => setAreaId(e.target.value)} style={selectStyle}>
          <option value="">No growth area</option>
          {areas.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
      )}
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button onClick={save} disabled={!!busy || !title.trim()} style={primaryBtn} data-testid="composer-save">{busy || (editTarget ? 'Update artifact' : 'Save artifact')}</button>
        <button onClick={onCancel} style={linkBtn}>Cancel</button>
      </div>
      {err && <Notice tone="error">{err}</Notice>}
      {picking && <EvidencePicker api={api} onPick={(e) => { setEvidence([...evidence, e]); setPicking(false) }} onClose={() => setPicking(false)} />}
    </div>
  )
}

/** Pick a PClaw source as evidence — projects, tasks, or knowledge items (read via the SDK). */
function EvidencePicker({ api, onPick, onClose }: {
  api: ReturnType<typeof createAppApi>; onPick: (e: Evidence) => void; onClose: () => void
}) {
  const [kind, setKind] = useState<'project' | 'task' | 'knowledge' | 'external'>('project')
  const [rows, setRows] = useState<Evidence[]>([])
  const [loading, setLoading] = useState(false)
  const [ext, setExt] = useState('')

  useEffect(() => {
    if (kind === 'external') { setRows([]); return }
    setLoading(true)
    const fetchRows = async (): Promise<Evidence[]> => {
      if (kind === 'project') {
        const d = await api.get<{ projects: { id: string; name: string }[] }>('/api/projects')
        return (d.projects || []).map((p) => ({ kind: 'project', ref: p.id, label: p.name }))
      }
      if (kind === 'task') {
        const d = await api.get<{ tasks: { id: string; title: string; status: string }[] }>('/api/tasks?status=done&limit=30')
        return (d.tasks || []).map((t) => ({ kind: 'task', ref: t.id, label: t.title }))
      }
      const d = await api.get<{ items: { id: string; title: string }[] }>('/api/knowledge/items?limit=30')
      return (d.items || []).map((k) => ({ kind: 'knowledge', ref: k.id, label: k.title }))
    }
    fetchRows().then(setRows).catch(() => setRows([])).finally(() => setLoading(false))
  }, [kind])

  return (
    <div style={{ ...cardStyle, cursor: 'default', background: 'var(--color-surface-high)', display: 'grid', gap: 8 }}>
      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
        <span style={{ fontSize: 12, opacity: 0.7 }}>Link evidence from:</span>
        {(['project', 'task', 'knowledge', 'external'] as const).map((k) => (
          <button key={k} onClick={() => setKind(k)} style={{ ...smallBtn, ...(kind === k ? tabActive : {}) }}>{KIND_META[k]?.label || k}</button>
        ))}
        <span style={{ flex: 1 }} />
        <button onClick={onClose} style={linkBtn}>close</button>
      </div>
      {kind === 'external' ? (
        <div style={{ display: 'flex', gap: 6 }}>
          <input value={ext} aria-label="External URL or reference" onChange={(e) => setExt(e.target.value)} placeholder="https://… or PR #123 / doc ref" style={inputStyle} />
          <button onClick={() => { if (ext.trim()) onPick({ kind: 'external', ref: ext.trim(), label: ext.trim().slice(0, 40) }) }} style={primaryBtn}>Add</button>
        </div>
      ) : loading ? <div style={{ fontSize: 12, opacity: 0.6 }}>Loading {kind}s…</div>
        : rows.length === 0 ? <div style={{ fontSize: 12, opacity: 0.6 }}>No {kind}s found.</div>
          : (
            <div style={{ display: 'grid', gap: 4, maxHeight: 220, overflow: 'auto' }}>
              {rows.map((e, i) => (
                <button key={i} onClick={() => onPick(e)} style={{ ...cardStyle, cursor: 'pointer', padding: 8, textAlign: 'left', fontSize: 12.5 }} data-testid="evidence-option">
                  {(KIND_META[e.kind]?.glyph || '🔗')} {e.label}
                </button>
              ))}
            </div>
          )}
    </div>
  )
}

// ── Sources: mine PClaw activity into candidate artifacts ────────────────────────────────
function Sources({ api, agent, artifacts, onChanged, onGoArtifacts }: {
  api: ReturnType<typeof createAppApi>; agent: ReturnType<typeof createAgentTask>; artifacts: Artifact[]; onChanged: () => void; onGoArtifacts: () => void
}) {
  const [candidates, setCandidates] = useState<{ kind: string; ref: string; title: string; subtitle: string; text: string }[] | null>(null)
  const [dismissed, setDismissed] = useState<Set<string>>(new Set())
  const [seed, setSeed] = useState<{ title: string; evidence: Evidence[]; sourceText: string } | null>(null)
  const [err, setErr] = useState('')

  // Refs already turned into an artifact — never re-offer them.
  const usedRefs = useMemo(() => new Set(artifacts.flatMap((a) => a.evidence.map((e) => `${e.kind}:${e.ref}`))), [artifacts])

  const mine = useCallback(async () => {
    setCandidates(null); setErr('')
    try {
      const [dis, projects, tasks] = await Promise.all([
        api.get<{ dismissed: string[] }>(`${A}/dismissed`).then((d) => d.dismissed).catch(() => []),
        api.get<{ projects: { id: string; name: string; status: string }[] }>('/api/projects').then((d) => d.projects || []).catch(() => []),
        api.get<{ tasks: { id: string; title: string; status: string }[] }>('/api/tasks?status=done&limit=25').then((d) => d.tasks || []).catch(() => []),
      ])
      setDismissed(new Set(dis))
      const cand: { kind: string; ref: string; title: string; subtitle: string; text: string }[] = []
      for (const p of projects) {
        if (p.name === 'Personal' || p.name === 'Repeatable') continue  // skip the protected defaults
        cand.push({ kind: 'project', ref: p.id, title: p.name, subtitle: `Project · ${p.status}`, text: `Project "${p.name}" (status ${p.status}) — an autonomous body of work you drove.` })
      }
      for (const t of tasks) {
        cand.push({ kind: 'task', ref: t.id, title: t.title, subtitle: 'Completed task', text: `Completed task: ${t.title}` })
      }
      setCandidates(cand)
    } catch (e) { setErr(String((e as Error).message || e)) }
  }, [])
  useEffect(() => { mine() }, [mine])

  const dismiss = async (ref: string) => {
    setDismissed(new Set([...dismissed, ref]))
    try { await api.post(`${A}/dismissed`, { ref }) } catch { /* non-fatal */ }
  }

  const visible = (candidates || []).filter((c) => {
    const ref = `${c.kind}:${c.ref}`
    return !dismissed.has(ref) && !usedRefs.has(ref)
  })

  if (seed) {
    return (
      <div>
        <button onClick={() => setSeed(null)} style={{ ...linkBtn, marginBottom: 8 }}>← Back to sources</button>
        <ArtifactComposer api={api} agent={agent} areas={[]} seed={seed}
          onDone={() => { setSeed(null); onChanged(); onGoArtifacts() }} onCancel={() => setSeed(null)} />
      </div>
    )
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <p style={{ fontSize: 12.5, opacity: 0.6, margin: 0 }}>Your real PClaw work — completed projects + tasks — as candidate artifacts. Draft one, or dismiss.</p>
        <button onClick={mine} style={smallBtn} data-testid="refresh-sources">Refresh</button>
      </div>
      {err && <Notice tone="error">{err}</Notice>}
      <Section title={candidates === null ? 'Mining your work…' : `Candidates (${visible.length})`}>
        {candidates === null ? <Notice>Reading your projects + tasks…</Notice>
          : visible.length === 0 ? <Notice>Nothing new to surface. Completed projects + tasks appear here as you do the work.</Notice>
            : visible.map((c) => (
              <div key={`${c.kind}:${c.ref}`} style={cardStyle} data-testid="source-candidate">
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <span style={{ flex: 1, fontWeight: 600, fontSize: 13.5 }}>{(KIND_META[c.kind]?.glyph || '🔗')} {c.title}</span>
                  <button onClick={() => setSeed({ title: c.title, evidence: [{ kind: c.kind, ref: c.ref, label: c.title }], sourceText: c.text })} style={primaryBtn} data-testid="draft-candidate">Draft artifact</button>
                  <button onClick={() => dismiss(`${c.kind}:${c.ref}`)} style={linkBtn} data-testid="dismiss-candidate">dismiss</button>
                </div>
                <div style={{ fontSize: 12, opacity: 0.6, marginTop: 2 }}>{c.subtitle}</div>
              </div>
            ))}
      </Section>
    </div>
  )
}

// ── Growth areas ─────────────────────────────────────────────────────────────────────────
function Areas({ api, areas, artifacts, readiness, onChanged }: {
  api: ReturnType<typeof createAppApi>; areas: Area[]; artifacts: Artifact[]; readiness: Readiness | null; onChanged: () => void
}) {
  const [name, setName] = useState('')
  const [target, setTarget] = useState('')
  const [dimension, setDimension] = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editName, setEditName] = useState('')
  const [editTarget, setEditTarget] = useState('')
  const [editDimension, setEditDimension] = useState('')
  const [editStatus, setEditStatus] = useState('')
  const dims = readiness?.dimensions.map((d) => d.dimension) || []

  const add = async () => {
    if (!name.trim()) return
    await api.post(`${A}/areas`, { name: name.trim(), target: target.trim(), dimension })
    setName(''); setTarget(''); setDimension(''); onChanged()
  }

  const startEdit = (a: Area) => {
    setEditingId(a.id); setEditName(a.name); setEditTarget(a.target)
    setEditDimension(a.dimension); setEditStatus(a.status)
  }

  const saveEdit = async () => {
    if (!editingId || !editName.trim()) return
    await api.patch(`${A}/areas/${editingId}`, { name: editName.trim(), target: editTarget.trim(), dimension: editDimension, status: editStatus })
    setEditingId(null); onChanged()
  }

  return (
    <div>
      <Section title="Define a growth area">
        <div style={{ display: 'grid', gap: 8 }}>
          <input value={name} aria-label="Growth area name" onChange={(e) => setName(e.target.value)} placeholder="e.g. Cross-team influence" style={inputStyle} data-testid="area-name" />
          <input value={target} aria-label="Target" onChange={(e) => setTarget(e.target.value)} placeholder="Target — what does success look like?" style={inputStyle} />
          <div style={{ display: 'flex', gap: 8 }}>
            <select value={dimension} aria-label="Rubric dimension" onChange={(e) => setDimension(e.target.value)} style={selectStyle}>
              <option value="">Any dimension</option>
              {dims.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
            <button onClick={add} disabled={!name.trim()} style={primaryBtn} data-testid="area-add">Add area</button>
          </div>
        </div>
      </Section>
      <Section title={`Growth areas (${areas.length})`}>
        {areas.length === 0 ? <Notice>No growth areas yet. Define what you're deliberately working toward.</Notice>
          : areas.map((a) => {
            const linked = artifacts.filter((x) => x.area_id === a.id)
            if (editingId === a.id) {
              return (
                <div key={a.id} style={{ ...cardStyle, cursor: 'default' }} data-testid="area-editing">
                  <div style={{ display: 'grid', gap: 8 }}>
                    <input value={editName} aria-label="Area name" onChange={(e) => setEditName(e.target.value)} style={inputStyle} />
                    <input value={editTarget} aria-label="Target" onChange={(e) => setEditTarget(e.target.value)} placeholder="Target" style={inputStyle} />
                    <div style={{ display: 'flex', gap: 8 }}>
                      <select value={editDimension} aria-label="Dimension" onChange={(e) => setEditDimension(e.target.value)} style={selectStyle}>
                        <option value="">Any dimension</option>
                        {dims.map((d) => <option key={d} value={d}>{d}</option>)}
                      </select>
                      <select value={editStatus} aria-label="Status" onChange={(e) => setEditStatus(e.target.value)} style={selectStyle}>
                        <option value="active">Active</option>
                        <option value="completed">Completed</option>
                        <option value="paused">Paused</option>
                      </select>
                    </div>
                    <div style={{ display: 'flex', gap: 8 }}>
                      <button onClick={saveEdit} disabled={!editName.trim()} style={primaryBtn} data-testid="area-save-edit">Save</button>
                      <button onClick={() => setEditingId(null)} style={linkBtn}>Cancel</button>
                    </div>
                  </div>
                </div>
              )
            }
            return (
              <div key={a.id} style={cardStyle} data-testid="area">
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <span style={{ flex: 1, fontWeight: 600, fontSize: 14 }}>{a.name}{a.dimension ? <span style={{ opacity: 0.5, fontWeight: 400, fontSize: 12 }}> · {a.dimension}</span> : null}</span>
                  <span style={{ fontSize: 11.5, opacity: 0.7 }}>{linked.length} artifact{linked.length === 1 ? '' : 's'}</span>
                  <button onClick={() => startEdit(a)} style={linkBtn} data-testid="area-edit">edit</button>
                  <button onClick={() => api.del(`${A}/areas/${a.id}`).then(onChanged)} style={linkBtn} data-testid="area-delete">delete</button>
                </div>
                {a.target && <div style={{ fontSize: 12.5, opacity: 0.75, marginTop: 3 }}>🎯 {a.target}</div>}
                {a.status && a.status !== 'active' && <div style={{ fontSize: 11, opacity: 0.6, marginTop: 2 }}>Status: {a.status}</div>}
                {linked.length === 0 && <div style={{ fontSize: 12, opacity: 0.55, marginTop: 4, fontStyle: 'italic' }}>No evidence yet — link an artifact to show progress.</div>}
                {linked.map((x) => <div key={x.id} style={{ fontSize: 12, opacity: 0.8, marginTop: 3 }}>• {x.title}</div>)}
              </div>
            )
          })}
      </Section>
    </div>
  )
}

// ── Digest (brag-doc) ──────────────────────────────────────────────────────────────────
function currentQuarter(): string {
  const d = new Date()
  return `${d.getFullYear()}-Q${Math.floor(d.getMonth() / 3) + 1}`
}

/** Models often wrap a "return Markdown" answer in a ```markdown fence despite
 *  instructions; the digest card renders raw text, so the fence showed literally.
 *  Unwrap ONE whole-document fence (any language tag); leave inner fences alone. */
function unwrapFence(text: string): string {
  const m = /^```[a-zA-Z]*\n([\s\S]*?)\n?```$/.exec(text)
  return m ? m[1].trim() : text
}

function DigestTab({ api, agent, digests, artifacts, areas, onChanged }: {
  api: ReturnType<typeof createAppApi>; agent: ReturnType<typeof createAgentTask>; digests: Digest[]; artifacts: Artifact[]; areas: Area[]; onChanged: () => void
}) {
  const [period, setPeriod] = useState(currentQuarter())
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')

  const generate = async () => {
    if (busy) return
    const scoped = artifacts.filter((a) => a.period === period)
    const use = scoped.length ? scoped : artifacts
    if (!use.length) { setStatus('No artifacts to summarize.'); return }
    setBusy(true); setStatus('Generating…')
    try {
      const areaName: Record<string, string> = Object.fromEntries(areas.map((a) => [a.id, a.name]))
      const payload = use.map((a) => ({ title: a.title, situation: a.situation, behavior: a.behavior, impact: a.impact, dimensions: a.dimensions, date: a.date, area: areaName[a.area_id] || '', evidence: a.evidence.map((e) => e.label || e.ref) }))
      const task = `Write a concise, professional growth digest ("brag doc") in Markdown for the period "${period}". Base it ONLY on these evidenced artifacts (JSON) — do NOT invent details or metrics. Group by growth area or dimension, lead each item with measurable impact, and cite the evidence in parentheses. Keep it tight + factual. Return ONLY the raw Markdown document — no code fences, no preamble or commentary. Artifacts: ${JSON.stringify(payload)}`
      const res = await agent.run(task, { maxTurns: 3 })
      const md = unwrapFence((res.result || '').trim())
      if (!md) throw new Error('empty digest')
      await api.post(`${A}/digests`, { period, content_md: md })
      setStatus(''); onChanged()
    } catch (e) { setStatus(String((e as Error).message || e)) } finally { setBusy(false) }
  }

  const copy = (t: string) => { try { void navigator.clipboard?.writeText(t) } catch { /* unavailable */ } }
  const toKnowledge = async (d: Digest) => {
    try { await api.post('/api/knowledge/items', { type: 'note', title: `Growth digest — ${d.period || 'all'}`, content: d.content_md }); setStatus('Exported to Knowledge.') }
    catch (e) { setStatus(String((e as Error).message || e)) }
  }

  return (
    <div>
      <Section title="Generate a digest">
        <div style={{ display: 'flex', gap: 8 }}>
          <input value={period} aria-label="Digest period" onChange={(e) => setPeriod(e.target.value)} placeholder="Period e.g. 2026-Q3" style={inputStyle} data-testid="digest-period" />
          <button onClick={generate} disabled={busy} style={primaryBtn} data-testid="digest-generate">{busy ? (status || 'Generating…') : 'Generate'}</button>
        </div>
        <p style={{ fontSize: 12, opacity: 0.6, marginTop: 4 }}>{status && !busy ? status : 'Summarizes your evidenced artifacts into a shareable accomplishment doc that cites its sources.'}</p>
      </Section>
      <Section title={`Digests (${digests.length})`}>
        {digests.length === 0 ? <Notice>No digests yet. Pick a period and generate one.</Notice>
          : digests.map((d) => (
            <div key={d.id} style={cardStyle} data-testid="digest">
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <span style={{ flex: 1, fontWeight: 600, fontSize: 14 }}>{d.period || '—'}</span>
                <span style={{ fontSize: 11, opacity: 0.6 }}>{(d.created_at || '').slice(0, 10)}</span>
                <button onClick={() => copy(d.content_md)} style={linkBtn}>copy</button>
                <button onClick={() => toKnowledge(d)} style={linkBtn} data-testid="digest-export">export → Knowledge</button>
                <button onClick={() => api.del(`${A}/digests/${d.id}`).then(onChanged)} style={linkBtn} data-testid="digest-delete">delete</button>
              </div>
              <div style={{ marginTop: 8, padding: 12, borderRadius: 'var(--radius-sm, 8px)', background: 'var(--color-surface-high)', whiteSpace: 'pre-wrap', fontSize: 13, lineHeight: 1.5 }}>{d.content_md}</div>
            </div>))}
      </Section>
    </div>
  )
}

// ── Settings: the rubric lens ──────────────────────────────────────────────────────────
function KeywordsInput({ value, onChange }: { value: string[]; onChange: (v: string[]) => void }) {
  const joined = (value || []).join(', ')
  const [text, setText] = useState(joined)
  const [focused, setFocused] = useState(false)
  useEffect(() => { if (!focused) setText(joined) }, [joined, focused])
  return (
    <input value={text} aria-label="Requirement keywords, comma-separated"
      onChange={(e) => { setText(e.target.value); onChange(e.target.value.split(',').map((s) => s.trim()).filter(Boolean)) }}
      onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
      placeholder="keywords, comma-separated" style={inputStyle} />
  )
}

function Settings({ api, onChanged }: { api: ReturnType<typeof createAppApi>; onChanged: () => void }) {
  const [rubric, setRubric] = useState<RubricDoc | null>(null)
  const [isOverride, setIsOverride] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    api.get<{ rubric: RubricDoc; is_override: boolean }>(`${A}/rubric`).then((d) => { setRubric(d.rubric); setIsOverride(d.is_override) }).catch((e) => setMsg(String(e.message || e)))
  }, [])
  if (!rubric) return <Notice>Loading rubric…</Notice>

  const saveRubric = async () => { try { await api.put(`${A}/rubric`, rubric); setIsOverride(true); setMsg('Rubric saved.'); onChanged() } catch (e) { setMsg(String((e as Error).message || e)) } }
  const resetRubric = async () => { try { const d = await api.post<{ rubric: RubricDoc }>(`${A}/rubric/reset`); setRubric(d.rubric); setIsOverride(false); setMsg('Reset to default.'); onChanged() } catch (e) { setMsg(String((e as Error).message || e)) } }
  const setDim = (i: number, v: string) => setRubric((r) => r && ({ ...r, dimensions: r.dimensions.map((d, j) => j === i ? v : d) }))
  const addDim = () => setRubric((r) => r && ({ ...r, dimensions: [...r.dimensions, 'New dimension'] }))
  const rmDim = (i: number) => setRubric((r) => r && ({ ...r, dimensions: r.dimensions.filter((_, j) => j !== i) }))
  const setReq = (i: number, patch: Partial<RubricReq>) => setRubric((r) => r && ({ ...r, requirements: r.requirements.map((q, j) => j === i ? { ...q, ...patch } : q) }))
  const addReq = () => setRubric((r) => r && ({ ...r, requirements: [...r.requirements, { code: `R${r.requirements.length + 1}`, dim: r.dimensions[0] || '', short: '', threshold: 1, keywords: [] }] }))
  const rmReq = (i: number) => setRubric((r) => r && ({ ...r, requirements: r.requirements.filter((_, j) => j !== i) }))

  return (
    <div>
      <Section title="Growth rubric (scoring lens)">
        <p style={{ fontSize: 12, opacity: 0.6, margin: '0 0 10px' }}>{isOverride ? 'Using your custom rubric.' : 'Using the built-in default.'} Dimensions + keyword requirements drive classification + readiness scoring — they don't limit what you can log.</p>
        <label style={labelText}>Label</label>
        <input value={rubric.label} aria-label="Rubric label" onChange={(e) => setRubric((r) => r && ({ ...r, label: e.target.value }))} style={{ ...inputStyle, width: '100%', marginBottom: 12 }} data-testid="rubric-label" />
        <label style={labelText}>Dimensions</label>
        <div style={{ display: 'grid', gap: 6, marginBottom: 12 }}>
          {rubric.dimensions.map((d, i) => (
            <div key={i} style={{ display: 'flex', gap: 6 }}>
              <input value={d} aria-label={`Dimension ${i + 1}`} onChange={(e) => setDim(i, e.target.value)} style={{ ...inputStyle, flex: 1 }} />
              <button onClick={() => rmDim(i)} style={smallBtn} aria-label="Remove dimension">×</button>
            </div>
          ))}
          <button onClick={addDim} style={smallBtn} data-testid="add-dim">+ Add dimension</button>
        </div>
        <label style={labelText}>Requirements</label>
        <div style={{ display: 'grid', gap: 8, marginBottom: 12 }}>
          {rubric.requirements.map((q, i) => (
            <div key={i} style={{ ...cardStyle, marginBottom: 0, display: 'grid', gap: 6 }}>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <input value={q.code} aria-label="Requirement code" onChange={(e) => setReq(i, { code: e.target.value })} placeholder="code" style={{ ...inputStyle, width: 80 }} />
                <select value={q.dim} aria-label="Requirement dimension" onChange={(e) => setReq(i, { dim: e.target.value })} style={{ ...selectStyle, flex: 1 }}>
                  {rubric.dimensions.map((d) => <option key={d} value={d}>{d}</option>)}
                </select>
                <span style={{ fontSize: 12, opacity: 0.6 }}>threshold</span>
                <input type="number" min={1} aria-label="Requirement threshold" value={q.threshold} onChange={(e) => setReq(i, { threshold: Math.max(1, Number(e.target.value) || 1) })} style={{ ...inputStyle, width: 60 }} />
                <button onClick={() => rmReq(i)} style={smallBtn} aria-label="Remove requirement">×</button>
              </div>
              <input value={q.short || ''} aria-label="Requirement short label" onChange={(e) => setReq(i, { short: e.target.value })} placeholder="short label" style={inputStyle} />
              <KeywordsInput value={q.keywords || []} onChange={(kw) => setReq(i, { keywords: kw })} />
            </div>
          ))}
          <button onClick={addReq} style={smallBtn} data-testid="add-req">+ Add requirement</button>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button onClick={saveRubric} style={primaryBtn} data-testid="save-rubric">Save rubric</button>
          <button onClick={resetRubric} style={linkBtn} data-testid="reset-rubric">Reset to default</button>
        </div>
      </Section>
      {msg && <Notice>{msg}</Notice>}
    </div>
  )
}

// ── style helpers — matched to the mainUI component spec (design/tokens.css + ui primitives):
// cards → radius-lg + surface-container; inputs → radius-md; buttons/tabs/chips → radius-pill;
// weight via fontVariationSettings "wght" (btn 470 / section 600 / active-tab 550); hover swaps
// primary→primary-emphasis + surface-high→highest; semantic tints via color-mix (no -container tokens).
const cardStyle: React.CSSProperties = { display: 'block', width: '100%', padding: 16, borderRadius: 'var(--radius-lg, 16px)', border: '1px solid color-mix(in srgb, var(--color-outline-variant) 40%, transparent)', background: 'var(--color-surface-container)', color: 'inherit', marginBottom: 8 }
const inputStyle: React.CSSProperties = { flex: 1, padding: '8px 12px', borderRadius: 'var(--radius-md, 12px)', border: 'none', background: 'var(--color-surface-high)', color: 'var(--color-on-surface)', fontSize: '0.9375rem', outline: 'none' }
const selectStyle: React.CSSProperties = { ...inputStyle, appearance: 'none', paddingRight: 30 }
const primaryBtn: React.CSSProperties = { padding: '0 20px', height: 40, borderRadius: 'var(--radius-pill, 9999px)', border: 'none', background: 'var(--color-primary)', color: 'var(--color-on-primary)', cursor: 'pointer', whiteSpace: 'nowrap', fontSize: '0.9375rem', fontVariationSettings: '"wght" 470', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 8, transition: 'background-color .1s cubic-bezier(0.2,0,0,1)' }
const linkBtn: React.CSSProperties = { background: 'none', border: 'none', color: 'var(--color-primary)', cursor: 'pointer', fontSize: '0.8125rem' }
const smallBtn: React.CSSProperties = { padding: '0 12px', height: 32, borderRadius: 'var(--radius-pill, 9999px)', border: 'none', background: 'var(--color-surface-high)', color: 'var(--color-on-surface)', cursor: 'pointer', fontSize: '0.8125rem', width: 'fit-content', display: 'inline-flex', alignItems: 'center', gap: 6, transition: 'background-color .15s' }
const tabStyle: React.CSSProperties = { padding: '0 12px', height: 32, borderRadius: 'var(--radius-pill, 9999px)', border: 'none', background: 'transparent', color: 'var(--color-on-surface-low)', cursor: 'pointer', fontSize: '0.8125rem', transition: 'color .15s, background-color .15s' }
const tabActive: React.CSSProperties = { background: 'var(--color-primary)', color: 'var(--color-on-primary)', fontVariationSettings: '"wght" 550' }
const labelText: React.CSSProperties = { fontSize: '0.7rem', textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--color-on-surface-low)', marginBottom: 4, display: 'block' }

function Header({ title, subtitle }: { title: string; subtitle?: string }) {
  return <div><h1 style={{ fontSize: '1.25rem', lineHeight: '1.5rem', fontVariationSettings: '"wght" 470', margin: 0, color: 'var(--color-on-surface)' }}>{title}</h1>{subtitle && <p style={{ color: 'var(--color-on-surface-low)', margin: '6px 0 0', fontSize: '0.8125rem' }}>{subtitle}</p>}</div>
}
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return <section style={{ margin: '24px 0' }}><h3 style={{ fontSize: '0.9375rem', fontVariationSettings: '"wght" 600', margin: '0 0 8px', color: 'var(--color-on-surface)' }}>{title}</h3>{children}</section>
}
function Notice({ children, tone }: { children: React.ReactNode; tone?: 'error' }) {
  return <div style={{ padding: 12, borderRadius: 'var(--radius-md, 12px)', fontSize: '0.8125rem', border: '1px solid var(--color-outline-variant)', background: 'var(--color-surface-high)', color: tone === 'error' ? 'var(--color-danger)' : 'var(--color-on-surface-low)' }}>{children}</div>
}
function Link({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return <button onClick={onClick} style={{ ...linkBtn, fontSize: 'inherit', padding: 0, textDecoration: 'underline' }}>{children}</button>
}

export function mount(el: HTMLElement, ctx: AppContext): () => void {
  const root: Root = createRoot(el)
  root.render(<App ctx={ctx} />)
  return () => root.unmount()
}
