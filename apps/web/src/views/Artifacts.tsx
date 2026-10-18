import { useState } from 'react'
import { useKernel, useQuery } from '../api.ts'
import { useI18n } from '../i18n.ts'
import type { Page, Selection } from '../App.tsx'
import { ErrorLine, Label, Section, short } from './common.tsx'

const TEXT = /^(text\/|application\/json)/

/** Artifact browser (FR-UI-11). */
export function ArtifactsView({ selection, select }: { selection: Selection; select: (s: Partial<Selection>, to?: Page) => void }) {
  const { t } = useI18n()
  const { client } = useKernel()
  const [type, setType] = useState('')
  const { data } = useQuery((c) => c.call('artifacts.list', { limit: 500, ...(type ? { type } : {}) }), [type])
  const { data: meta } = useQuery((c) => (selection.artifact ? c.call('artifacts.stat', { hash: selection.artifact }) : Promise.resolve(undefined)), [selection.artifact], false)
  const { data: content, error } = useQuery(
    async () => (meta && TEXT.test(meta.mediaType) && meta.size < 2_000_000 ? (await client.download(meta.hash)).text() : undefined),
    [meta?.hash],
    false,
  )
  // Images such as the rendered layout (FR-PHY-05) are shown inline.
  const { data: image } = useQuery(
    async () => (meta && meta.mediaType.startsWith('image/') && meta.label !== 'nda' ? URL.createObjectURL(await (await client.download(meta.hash)).blob()) : undefined),
    [meta?.hash],
    false,
  )
  const [hash, setHash] = useState('')
  return (
    <>
      {meta && (
        <Section
          title={`${meta.type} · ${short(meta.hash)}`}
          actions={<button onClick={() => select({}, 'trajectory')}>{t('artifacts.lineage')}</button>}
        >
          <dl className="facts">
            <dt>{t('common.hash')}</dt>
            <dd className="mono">{meta.hash}</dd>
            <dt>{t('common.label')}</dt>
            <dd>
              <Label label={meta.label} />
            </dd>
            <dt>{t('common.size')}</dt>
            <dd>{meta.size.toLocaleString()} B</dd>
            <dt>{t('common.actor')}</dt>
            <dd>
              {meta.creator.kind}:{meta.creator.id}
            </dd>
          </dl>
          {meta.label === 'nda' && <p className="muted">{t('artifacts.ndaHidden')}</p>}
          <ErrorLine error={error} />
          {content !== undefined && <pre className="content">{content.slice(0, 50_000)}</pre>}
          {image && <img className="layout" src={image} alt={meta.type} />}
        </Section>
      )}
      <Section
        title={t('nav.artifacts')}
        actions={
          <>
            {/* A hash from a package manifest opens that artifact and its lineage (AC-08). */}
            <input placeholder={t('artifacts.openHash')} value={hash} onChange={(e) => setHash(e.target.value.trim())} onKeyDown={(e) => e.key === 'Enter' && /^[0-9a-f]{64}$/.test(hash) && select({ artifact: hash })} />
            <input placeholder={t('artifacts.type')} value={type} onChange={(e) => setType(e.target.value)} />
          </>
        }
      >
        <table>
          <thead>
            <tr>
              <th>{t('common.hash')}</th>
              <th>{t('artifacts.type')}</th>
              <th>{t('common.label')}</th>
              <th>{t('common.size')}</th>
            </tr>
          </thead>
          <tbody>
            {data?.artifacts
              .slice()
              .reverse()
              .map((a) => (
                <tr key={a.hash} className={a.hash === selection.artifact ? 'selected clickable' : 'clickable'} onClick={() => select({ artifact: a.hash })}>
                  <td className="mono">{short(a.hash)}</td>
                  <td>{a.type}</td>
                  <td>
                    <Label label={a.label} />
                  </td>
                  <td>{a.size.toLocaleString()}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </Section>
    </>
  )
}
