import { useMemo, useState } from 'react'
import type { ArtifactMeta, RunRecord } from '@llmchip/kernel-api'
import { useKernel, useQuery } from '../api.ts'
import { useI18n, type Key } from '../i18n.ts'
import type { Page, Selection } from '../App.tsx'
import { typeDescription, typeName } from './artifactTypes.ts'
import { ErrorLine, Section, short } from './common.tsx'
import { RUN_TITLES } from './flowSteps.ts'
import { L0Outputs, TestSuite, TextIr } from './steps/S1.tsx'

const TEXT = /^(text\/|application\/json)/

type Json = any

/** Artifact browser (FR-UI-11): what an artifact is, where it came from, and its content in readable form. */
export function ArtifactsView({ selection, select }: { selection: Selection; select: (s: Partial<Selection>, to?: Page) => void }) {
  const { t } = useI18n()
  const { client } = useKernel()
  const { data } = useQuery((c) => c.call('artifacts.list', { limit: 1000 }), [])
  const { data: runs } = useQuery((c) => c.call('runs.list', { limit: 1000 }), [])
  const runById = useMemo(() => new Map((runs?.runs ?? []).map((r) => [r.id, r])), [runs])
  const { data: meta } = useQuery((c) => (selection.artifact ? c.call('artifacts.stat', { hash: selection.artifact }) : Promise.resolve(undefined)), [selection.artifact], false)
  const { data: content, error } = useQuery(
    async () => (meta && TEXT.test(meta.mediaType) && meta.size < 20_000_000 && meta.label !== 'nda' ? (await client.download(meta.hash)).text() : undefined),
    [meta?.hash],
    false,
  )
  // Images such as the rendered layout (FR-PHY-05) are shown inline.
  const { data: image } = useQuery(
    async () => (meta && meta.mediaType.startsWith('image/') && meta.label !== 'nda' ? URL.createObjectURL(await (await client.download(meta.hash)).blob()) : undefined),
    [meta?.hash],
    false,
  )
  const [filter, setFilter] = useState('')
  const [hash, setHash] = useState('')
  const all = (data?.artifacts ?? []).slice().reverse()
  const shown = all.filter((a) => !filter || typeName(t, a.type).includes(filter) || a.type.includes(filter) || (runById.get(a.run ?? '')?.project ?? '').includes(filter))
  const list = (
    <table>
      <thead>
        <tr>
          <th>{t('art.col.name')}</th>
          <th>{t('art.col.madeBy')}</th>
          <th>{t('art.col.project')}</th>
          <th>{t('art.col.time')}</th>
          <th>{t('common.size')}</th>
        </tr>
      </thead>
      <tbody>
        {shown.slice(0, 300).map((a) => {
          const run = runById.get(a.run ?? '')
          return (
            <tr key={a.hash} className={a.hash === selection.artifact ? 'selected clickable' : 'clickable'} onClick={() => select({ artifact: a.hash })}>
              <td>
                <strong>{typeName(t, a.type)}</strong>
              </td>
              <td>{madeBy(t, a, run)}</td>
              <td>{run?.project ?? '—'}</td>
              <td className="muted">{new Date(a.createdAt).toLocaleString()}</td>
              <td>{bytes(a.size)}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
  return (
    <>
      {meta && (
        <Section title={typeName(t, meta.type)} actions={<button onClick={() => select({}, 'trajectory')}>{t('artifacts.lineage')}</button>}>
          <p className="flow-what">{typeDescription(t, meta.type)}</p>
          <dl className="facts">
            <dt>{t('art.madeBy')}</dt>
            <dd>{madeBy(t, meta, runById.get(meta.run ?? ''))}</dd>
            <dt>{t('art.project')}</dt>
            <dd>{runById.get(meta.run ?? '')?.project ?? '—'}</dd>
            <dt>{t('art.time')}</dt>
            <dd>{new Date(meta.createdAt).toLocaleString()}</dd>
            <dt>{t('common.size')}</dt>
            <dd>{bytes(meta.size)}</dd>
            <dt>{t('art.label')}</dt>
            <dd>{t(`art.labelText.${meta.label}` as Key)}</dd>
            {meta.inputs.length > 0 && (
              <>
                <dt>{t('art.inputs')}</dt>
                <dd>
                  <Inputs hashes={meta.inputs} select={select} />
                </dd>
              </>
            )}
            <dt>{t('art.fingerprint')}</dt>
            <dd>
              <code title={meta.hash}>{short(meta.hash)}</code> <small className="muted">{t('art.fingerprintNote')}</small>
            </dd>
          </dl>
          {meta.label === 'nda' && <p className="muted">{t('artifacts.ndaHidden')}</p>}
          <ErrorLine error={error} />
          {image && <img className="layout" src={image} alt={typeName(t, meta.type)} />}
          {content !== undefined && <Content type={meta.type} text={content} />}
          {content === undefined && !image && meta.label !== 'nda' && <p className="muted">{t('art.binary', { size: bytes(meta.size) })}</p>}
        </Section>
      )}
      <Section
        title={t('nav.artifacts')}
        actions={
          <>
            {/* A hash from a package manifest opens that artifact and its lineage (AC-08). */}
            <input placeholder={t('artifacts.openHash')} value={hash} onChange={(e) => setHash(e.target.value.trim())} onKeyDown={(e) => e.key === 'Enter' && /^[0-9a-f]{64}$/.test(hash) && select({ artifact: hash })} />
            <input placeholder={t('art.filter')} value={filter} onChange={(e) => setFilter(e.target.value)} />
          </>
        }
      >
        <p className="muted">{t('art.listIntro', { count: all.length })}</p>
        {meta ? (
          <details>
            <summary>{t('art.showList', { count: shown.length })}</summary>
            {list}
          </details>
        ) : (
          list
        )}
      </Section>
    </>
  )
}

function bytes(n: number): string {
  return n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : n >= 1e3 ? `${(n / 1e3).toFixed(1)} KB` : `${n} B`
}

function madeBy(t: (k: Key, p?: Record<string, unknown>) => string, a: ArtifactMeta, run: RunRecord | undefined): string {
  if (run) {
    const key = RUN_TITLES[`${run.plugin}/${run.handler}`]
    return key ? t(key) : `${run.plugin}/${run.handler}`
  }
  if (a.run) return t('art.byRun')
  return a.creator.kind === 'human' ? t('art.byHuman') : a.creator.kind === 'plugin' ? t('art.byPluginData', { plugin: a.creator.id }) : t('art.byKernel')
}

function Inputs({ hashes, select }: { hashes: string[]; select: (s: Partial<Selection>, to?: Page) => void }) {
  const { t } = useI18n()
  const { data } = useQuery((c) => Promise.all(hashes.map((hash) => c.call('artifacts.stat', { hash }))), [hashes.join()], false)
  return (
    <span className="flow-outputs">
      {(data ?? []).map((m) => (
        <button key={m.hash} className="link" onClick={() => select({ artifact: m.hash })}>
          {typeName(t, m.type)}
        </button>
      ))}
    </span>
  )
}

/** A readable view of the content when there is one; the raw text otherwise, folded away. */
function Content({ type, text }: { type: string; text: string }) {
  const { t } = useI18n()
  let doc: Json
  try {
    doc = type.endsWith('.summary') || type === 'run.log' ? undefined : JSON.parse(text)
  } catch {
    doc = undefined
  }
  const view =
    doc && type === 'model.structure' ? (
      <StructureView doc={doc} />
    ) : doc && type === 'l0.outputs' ? (
      <L0PortView doc={doc} />
    ) : doc && type === 'l0g.outputs' ? (
      <L0Outputs outputs={doc} />
    ) : doc && type === 'upstream.report' && doc.items ? (
      <TestSuite suite={doc} />
    ) : doc && type === 'model.graph' && doc.supported ? (
      <TextIr graph={doc} />
    ) : doc && type === 'model.census' ? (
      <CensusView doc={doc} />
    ) : doc && type === 'model.bundle' ? (
      <BundleView doc={doc} />
    ) : undefined
  if (!doc) {
    return <pre className="content">{text.slice(0, 50_000)}</pre>
  }
  return (
    <>
      {view ?? <p className="muted">{t('art.noView')}</p>}
      <details className="s0-raw">
        <summary>{t('art.raw')}</summary>
        <pre className="content">{text.slice(0, 200_000)}</pre>
      </details>
    </>
  )
}

const CONFIG_KEYS: [string, Key][] = [
  ['dim', 's1.h.hidden'], ['hidden_dim', 's1.h.intermediate'], ['n_layers', 's1.h.layers'], ['n_heads', 's1.h.heads'], ['n_kv_heads', 's1.h.kvHeads'],
  ['head_size', 's1.h.headDim'], ['kv_dim', 'art.kvDim'], ['vocab_size', 's1.h.vocab'], ['seq_len', 'art.seqLen'], ['shared_classifier', 's1.h.tied'],
  ['norm_eps', 's1.h.normEps'], ['rope_theta', 's1.h.ropeTheta'],
]
const LLAMA2C_TENSORS: Record<string, Key> = {
  token_embedding: 's1.node.embed', rms_att: 's1.node.attn_norm', wq: 's1.node.q_proj', wk: 's1.node.k_proj', wv: 's1.node.v_proj', wo: 's1.node.o_proj',
  rms_ffn: 's1.node.mlp_norm', w1: 's1.node.gate_proj', w2: 's1.node.down_proj', w3: 's1.node.up_proj', rms_final: 's1.node.final_norm', wcls: 's1.node.lm_head',
}
const OPERATORS: Record<string, Key> = {
  rmsnorm: 'art.op.rmsnorm', matmul: 'art.op.matmul', rope: 's1.node.rope', attention: 's1.node.attention', swiglu: 'art.op.swiglu', embedding_lookup: 's1.node.embed',
}

/** model.structure (llama2.c intake): the model's configuration, operators and parameters. */
function StructureView({ doc }: { doc: Json }) {
  const { t } = useI18n()
  const fmt = (v: unknown) =>
    typeof v === 'boolean' ? t(v ? 'common.yes' : 'common.no') : typeof v === 'number' ? (v !== 0 && Math.abs(v) < 1e-3 ? v.toExponential() : v.toLocaleString()) : String(v)
  return (
    <>
      <p className="s0-formula">
        {t('art.structureSummary', { family: doc.family, params: doc.parameters.total.toLocaleString(), flops: doc.flopsPerToken.toLocaleString(), license: doc.license })}
      </p>
      <h4>{t('art.structureConfig')}</h4>
      <dl className="facts">
        {CONFIG_KEYS.filter(([k]) => doc.config[k] !== undefined).map(([k, label]) => (
          <div key={k} style={{ display: 'contents' }}>
            <dt>{t(label)}</dt>
            <dd>{fmt(doc.config[k])}</dd>
          </div>
        ))}
        {doc.tokenizer && (
          <>
            <dt>{t('art.tokenizerVocab')}</dt>
            <dd>{fmt(doc.tokenizer.vocabSize)}</dd>
          </>
        )}
      </dl>
      <h4>{t('art.structureOps')}</h4>
      <table className="s0-table">
        <tbody>
          {Object.entries(doc.operators as Record<string, number>).map(([op, n]) => (
            <tr key={op}>
              <td>{t(OPERATORS[op] ?? 's1.node.other')}</td>
              <td>{t('art.times', { n })}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h4>{t('art.structureParams')}</h4>
      <table className="s0-table">
        <tbody>
          {Object.entries(doc.parameters.by_tensor as Record<string, number>).map(([name, n]) => (
            <tr key={name}>
              <td>{t(LLAMA2C_TENSORS[name] ?? 's1.node.other')}</td>
              <td>
                <code>{name}</code>
              </td>
              <td>{n.toLocaleString()}</td>
              <td>{((100 * n) / doc.parameters.total).toFixed(1)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

/** l0.outputs (the run.c port's L0): what each prompt generates, and the perplexity. */
function L0PortView({ doc }: { doc: Json }) {
  const { t } = useI18n()
  return (
    <>
      <p className="s0-formula">{t('art.l0Summary', { count: doc.generations.length, ppl: doc.perplexity === null ? '—' : Number(doc.perplexity).toFixed(2) })}</p>
      <p className="muted">{t('art.l0Implementation')}</p>
      <table className="s0-table">
        <thead>
          <tr>
            <th>{t('s1.col.prompt')}</th>
            <th>{t('art.generated')}</th>
          </tr>
        </thead>
        <tbody>
          {doc.generations.map((g: Json, i: number) => (
            <tr key={i}>
              <td>{g.prompt}</td>
              <td className="generated">{g.text}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

function CensusView({ doc }: { doc: Json }) {
  const { t } = useI18n()
  const v = (k: string) => doc.hyper[k]?.value ?? '?'
  return (
    <>
      <p className="s0-formula">
        {t('s0.censusSummary', { tensors: doc.totals.tensors.toLocaleString(), params: doc.totals.parameters.toLocaleString(), layers: v('layers'), hidden: v('hidden'), vocab: v('vocab') })}
      </p>
      <ul className="flow-checks">
        {doc.checks.map((c: Json) => (
          <li key={c.id}>
            <span className={`pill ${c.ok ? 'ok' : 'bad'}`}>{t(c.ok ? 'flow.verdict.pass' : 'flow.verdict.fail')}</span>
            {t(`art.check.${c.id}` as Key)}
          </li>
        ))}
      </ul>
    </>
  )
}

function BundleView({ doc }: { doc: Json }) {
  const { t } = useI18n()
  return (
    <>
      <p className="s0-formula">{t('s0.importIntro', { name: doc.name, source: doc.source, format: doc.weightFormat })}</p>
      <table className="s0-table">
        <tbody>
          {doc.files.map((f: Json) => (
            <tr key={f.name}>
              <td>
                <code>{f.name}</code>
              </td>
              <td>{t(`s0.role.${f.role}` as Key)}</td>
              <td>{bytes(f.bytes)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
